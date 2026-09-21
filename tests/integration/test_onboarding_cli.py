"""CLI-level goal-only onboarding: `propose`, and the fail-closed acceptance gate
shared by discover/replay/invoke. Real subprocesses, a real fixture workstation,
and a real local HTTP model server -- no live model service.
"""

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from computer_use_replay.contracts import Capability, GoalRequest
from computer_use_replay.demo import serve_demo

PROPOSAL_ARGS = {
    "name": "read_savings_auto",
    "inputs": ["member_id"],
    "outputs": [{"name": "available_balance", "source": "balance", "kind": "money"}],
    "success_screen": "savings_screen",
}

DISCOVERY_PLAN = [
    ("fill_control", {"target": "member_input", "parameter": "member_id"}),
    ("click_control", {"target": "search"}),
    ("click_control", {"target": "open_member"}),
    ("click_control", {"target": "accounts"}),
    ("click_control", {"target": "savings"}),
    ("read_control", {"target": "balance", "output": "available_balance"}),
]


def _ollama_payload(name, arguments):
    body = {
        "done": True,
        "done_reason": "stop",
        "message": {"tool_calls": [{"function": {"name": name, "arguments": arguments}}]},
        "prompt_eval_count": 1,
        "eval_count": 1,
    }
    return json.dumps(body).encode()


class _Handler(BaseHTTPRequestHandler):
    """Answers a propose_contract call first, then plays a fixed discovery plan --
    the same shape tests/provider_samples.py's wire_server uses for discovery alone.
    """

    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        tool_names = {t["function"]["name"] for t in body["tools"]}
        if "propose_contract" in tool_names:
            name, args = "propose_contract", PROPOSAL_ARGS
        else:
            context = json.loads(body["messages"][-1]["content"])
            name, args = DISCOVERY_PLAN[len(context["history"])]
        payload = _ollama_payload(name, args)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def model_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


async def command(*args, stdin=asyncio.subprocess.DEVNULL):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "computer_use_replay.cli",
        *map(str, args),
        stdin=stdin,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(process.communicate(), 20)
    return process.returncode, out, err


async def test_propose_writes_a_reviewable_draft(tmp_path, model_server):
    out_path = tmp_path / "draft.request.json"
    code, out, err = await command(
        "propose",
        "--goal",
        "Look up a member and read their available savings balance.",
        "--model-url",
        model_server,
        "--model",
        "fixture-model",
        "--out",
        out_path,
        "--evidence",
        tmp_path / "runs",
    )
    assert code == 0, err.decode()
    request = GoalRequest.model_validate_json(out_path.read_bytes())
    assert request.name == "read_savings_auto"
    assert request.review.status == "draft"
    assert request.review.proposed_by == "ollama/fixture-model"
    # Printed to stdout for the operator to read before accepting; never to a file
    # they might not check.
    assert json.loads(out)["name"] == "read_savings_auto"
    assert b"draft" in err.lower() or b"Draft" in err


async def test_propose_reports_model_failure_without_opening_anything(tmp_path):
    code, out, err = await command(
        "propose",
        "--goal",
        "Look up a member and read their available savings balance.",
        "--model-url",
        "http://127.0.0.1:1",
        "--model",
        "fixture-model",
        "--model-retries",
        "0",
        "--out",
        tmp_path / "draft.request.json",
        "--evidence",
        tmp_path / "runs",
    )
    assert code == 1, err.decode()
    result = json.loads(out)
    assert result["status"] == "failure"
    assert result["failure"]["code"] == "model_unavailable"
    assert not (tmp_path / "draft.request.json").exists()


async def test_goal_only_discover_proposes_then_discovers_in_one_command(tmp_path, model_server):
    artifact_path = tmp_path / "artifact.json"
    async with serve_demo("normal") as (origin, app):
        code, out, err = await command(
            "discover",
            "--target",
            origin,
            "--model-url",
            model_server,
            "--model",
            "fixture-model",
            "--goal",
            "Look up a member and read their available savings balance.",
            "--inputs",
            '{"member_id":"00123"}',
            "--artifact",
            artifact_path,
            "--evidence",
            tmp_path / "runs",
            "--accept-draft",
        )
        assert code == 0, err.decode()
        assert app.state.finalizations == 0
    result = json.loads(out)
    assert result["status"] == "success"
    artifact = Capability.model_validate_json(artifact_path.read_bytes())
    assert artifact.name == "read_savings_auto"
    assert artifact.provenance.accepted_by == "flag"
    (events_path,) = (tmp_path / "runs").glob("*/events.jsonl")
    events = [json.loads(row) for row in events_path.read_text().splitlines()]
    accepted = [row for row in events if row["event"] == "contract_accepted"]
    assert len(accepted) == 1 and accepted[0]["code"] == "flag"
    proposed = [row for row in events if row["event"] == "contract_proposed"]
    assert len(proposed) == 1 and proposed[0]["capability"] == "read_savings_auto"


async def test_goal_only_discover_present_captions_the_proposal_before_the_tour(
    tmp_path, model_server, monkeypatch
):
    """--present on the goal-only path: before the ordinary per-step "model
    chose: ..." captions, the very first caption is the proposed/accepted
    contract summary -- driven in-process so the real overlay call can be spied.
    """
    from argparse import Namespace

    from computer_use_replay import cli
    from computer_use_replay.browser import BrowserSurface

    monkeypatch.setenv("COMPUTER_USE_REPLAY_HEADLESS", "1")
    captions = []
    original_present_outcome = BrowserSurface.present_outcome

    async def spy(self, text):
        captions.append(text)
        return await original_present_outcome(self, text)

    monkeypatch.setattr(BrowserSurface, "present_outcome", spy)

    async with serve_demo("normal") as (origin, app):
        args = Namespace(
            command="discover",
            request=None,
            goal="Look up a member and read their available savings balance.",
            target=origin,
            binding="profiles/juniper.json",
            presentation=None,
            evidence=str(tmp_path / "runs"),
            artifact=str(tmp_path / "artifact.json"),
            inputs="{}",
            input=[("member_id", "00123")],
            input_env=None,
            accept_draft=True,
            human=False,
            present=True,
            pace=0,
            provider="ollama",
            model="fixture-model",
            model_url=model_server,
            model_timeout=90,
            model_retries=2,
            model_decision_retries=1,
            model_max_tokens=2048,
            capabilities="capabilities",
        )
        code = await cli.run(args)
        assert app.state.finalizations == 0
    assert code == 0
    assert captions, "expected the proposal summary caption to be shown"
    assert captions[0].startswith("proposed: read_savings_auto")
    assert "inputs member_id" in captions[0]
    assert "outputs available_balance" in captions[0]
    assert "accepted: flag" in captions[0]


async def test_goal_only_discover_requires_a_goal(tmp_path):
    code, out, err = await command(
        "discover",
        "--inputs",
        '{"member_id":"00123"}',
        "--evidence",
        tmp_path / "runs",
    )
    assert code == 2, err.decode()


async def test_draft_request_is_refused_without_acceptance(tmp_path, model_server):
    draft_path = tmp_path / "draft.request.json"
    code, _, err = await command(
        "propose",
        "--goal",
        "Look up a member and read their available savings balance.",
        "--model-url",
        model_server,
        "--model",
        "fixture-model",
        "--out",
        draft_path,
        "--evidence",
        tmp_path / "runs",
    )
    assert code == 0, err.decode()
    async with serve_demo("normal") as (origin, app):
        code, out, err = await command(
            "discover",
            "--request",
            draft_path,
            "--target",
            origin,
            "--inputs",
            '{"member_id":"00123"}',
            "--artifact",
            tmp_path / "artifact.json",
            "--evidence",
            tmp_path / "runs2",
        )
        assert app.state.finalizations == 0
    assert code == 2, err.decode()
    assert not (tmp_path / "artifact.json").exists()
    assert b"draft_not_accepted" in out + err or b"Invalid configuration" in err


async def test_draft_request_accepted_with_the_flag_runs_and_records_acceptance(
    tmp_path, model_server
):
    draft_path = tmp_path / "draft.request.json"
    code, _, err = await command(
        "propose",
        "--goal",
        "Look up a member and read their available savings balance.",
        "--model-url",
        model_server,
        "--model",
        "fixture-model",
        "--out",
        draft_path,
        "--evidence",
        tmp_path / "runs",
    )
    assert code == 0, err.decode()
    artifact_path = tmp_path / "artifact.json"
    async with serve_demo("normal") as (origin, app):
        code, out, err = await command(
            "discover",
            "--request",
            draft_path,
            "--target",
            origin,
            "--model-url",
            model_server,
            "--model",
            "fixture-model",
            "--inputs",
            '{"member_id":"00123"}',
            "--artifact",
            artifact_path,
            "--evidence",
            tmp_path / "runs3",
            "--accept-draft",
        )
        assert app.state.finalizations == 0
    assert code == 0, err.decode()
    artifact = Capability.model_validate_json(artifact_path.read_bytes())
    assert artifact.provenance.accepted_by == "flag"


async def test_draft_accepted_interactively_is_recorded_as_tty(tmp_path, model_server, monkeypatch):
    """Exercises cli.run()'s interactive-acceptance branch directly: no --accept-draft,
    confirm_draft() (the interactive y/N prompt) returns True. The subprocess-level
    tests above cover --accept-draft (code "flag") and refusal off a TTY; a real TTY
    is not available in the test harness, so this drives cli.run() in-process with
    confirm_draft() patched to simulate a person answering "y".
    """
    from argparse import Namespace
    from pathlib import Path

    from computer_use_replay import cli
    from computer_use_replay.onboarding import Proposal, draft_request
    from computer_use_replay.policy import Binding

    binding = Binding.load(Path("profiles/juniper.json"))
    proposal = Proposal(
        name="lookup_member_savings",
        inputs=("member_id",),
        outputs={"available_balance": ("balance", "money")},
        success_screen="savings_screen",
    )
    request = draft_request(
        binding,
        "Look up a member and read their available savings balance.",
        proposal,
        proposed_by="ollama/fixture-model",
    )
    draft_path = tmp_path / "draft.request.json"
    draft_path.write_text(request.model_dump_json())

    async def accept(_request):
        return True

    monkeypatch.setattr("computer_use_replay.onboarding.confirm_draft", accept)

    async with serve_demo("normal") as (origin, app):
        args = Namespace(
            command="discover",
            request=str(draft_path),
            goal=None,
            target=origin,
            binding="profiles/juniper.json",
            presentation=None,
            evidence=str(tmp_path / "runs"),
            artifact=str(tmp_path / "artifact.json"),
            inputs='{"member_id":"00123"}',
            accept_draft=False,
            human=False,
            provider="ollama",
            model="fixture-model",
            model_url=model_server,
            model_timeout=90,
            model_retries=2,
            model_decision_retries=1,
            model_max_tokens=2048,
            capabilities="capabilities",
        )
        code = await cli.run(args)
        assert app.state.finalizations == 0
    assert code == 0
    artifact = Capability.model_validate_json((tmp_path / "artifact.json").read_bytes())
    assert artifact.provenance.accepted_by == "tty"
    (events_path,) = (tmp_path / "runs").glob("*/events.jsonl")
    events = [json.loads(row) for row in events_path.read_text().splitlines()]
    accepted = [row for row in events if row["event"] == "contract_accepted"]
    assert len(accepted) == 1 and accepted[0]["code"] == "tty"


async def test_hand_written_request_never_hits_the_acceptance_gate(tmp_path):
    async with serve_demo("normal") as (origin, app):
        code, out, err = await command(
            "replay",
            "--target",
            origin,
            "--artifact",
            "capabilities/read_savings.json",
            "--inputs",
            '{"member_id":"00456"}',
            "--evidence",
            tmp_path,
        )
        assert app.state.finalizations == 0
    assert code == 0, err.decode()
    (events_path,) = tmp_path.glob("*/events.jsonl")
    events = [json.loads(row) for row in events_path.read_text().splitlines()]
    assert not any(row["event"] == "contract_accepted" for row in events)
