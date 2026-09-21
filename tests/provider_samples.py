"""Hand-authored protocol fixtures. These are not live model responses or quality evidence."""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx


def response_for(provider, name="finish", arguments=None):
    args = {} if arguments is None else arguments
    if provider == "ollama":
        return {
            "done": True,
            "done_reason": "stop",
            "message": {"tool_calls": [{"function": {"name": name, "arguments": args}}]},
            "prompt_eval_count": 120,
            "eval_count": 20,
        }
    if provider == "openai":
        return {
            "status": "completed",
            "output": [
                {"type": "reasoning", "summary": []},
                {
                    "type": "function_call",
                    "status": "completed",
                    "name": name,
                    "call_id": "fixture-call",
                    "arguments": json.dumps(args),
                },
            ],
            "usage": {"input_tokens": 120, "output_tokens": 20},
        }


@contextmanager
def wire_server(provider, *, premature_finish=False):
    requests = []
    plan = [
        ("fill_control", {"target": "member_input", "parameter": "member_id"}),
        ("click_control", {"target": "search"}),
        ("click_control", {"target": "open_member"}),
        ("click_control", {"target": "accounts"}),
        ("click_control", {"target": "savings"}),
        ("read_control", {"target": "balance", "output": "available_balance"}),
    ]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            # A temporary outage after the fill tests retrying a prediction at a real UI boundary.
            if len(requests) == 2:
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if provider == "openai":
                context = json.loads(body["input"])
            else:
                context = json.loads(body["messages"][-1]["content"])
            name, args = ("finish", {}) if premature_finish else plan[len(context["history"])]
            payload = json.dumps(response_for(provider, name, args)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class FixtureTransport(httpx.AsyncHTTPTransport):
    """Only test injection reroutes official hosts to the real loopback fixture server."""

    def __init__(self, origin):
        super().__init__()
        self.origin = origin

    async def handle_async_request(self, request):
        request.url = httpx.URL(self.origin + request.url.path)
        return await super().handle_async_request(request)
