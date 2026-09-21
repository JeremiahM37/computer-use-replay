"""CLI process boundaries, caller validation, and fixture endpoints."""

import asyncio
import json
import os
import subprocess
import sys

import httpx
import pytest

from computer_use_replay.contracts import Capability, Input
from computer_use_replay.demo import create_app, serve_demo
from computer_use_replay.engine import Replay


@pytest.mark.parametrize("tenant_b", [False, True])
async def test_installed_cli_replays_without_importing_planner(tmp_path, capability, tenant_b):
    artifact_path = tmp_path / "capability.json"
    artifact_path.write_text(capability.model_dump_json())
    # Run the real CLI in another interpreter. Any planner import is a hard error.
    code = """import sys
class NoPlanner:
 def find_spec(self,fullname,path=None,target=None):
  if fullname in {'computer_use_replay.planner','computer_use_replay.discovery','computer_use_replay.providers'}:
   raise RuntimeError('Replay attempted to import model discovery')
sys.meta_path.insert(0,NoPlanner())
from computer_use_replay.cli import main
main()
"""
    async with serve_demo("tenant_b" if tenant_b else "normal") as (origin, _):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            code,
            "replay",
            *(["--presentation", "profiles/tenant_b.json"] if tenant_b else []),
            "--target",
            origin,
            "--artifact",
            str(artifact_path),
            "--inputs",
            '{"member_id":"00456"}',
            "--evidence",
            str(tmp_path / "runs"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "OLLAMA_URL": "http://127.0.0.1:1"},
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 15)
    assert process.returncode == 0, stderr.decode()
    result = json.loads(stdout)
    assert result["outputs"]["available_balance"]["amount"] == "8902.10"
    assert result["llm_calls"] == 0
    assert Capability.model_validate_json(artifact_path.read_text()) == capability


async def test_catalog_lists_then_invoke_by_name_replays_without_a_model(tmp_path):
    # Same "no planner/discovery import" guard as the replay test above: invoke
    # reuses the ordinary replay path (cli.py:run) rather than a separate engine.
    code = """import sys
class NoPlanner:
 def find_spec(self,fullname,path=None,target=None):
  if fullname in {'computer_use_replay.planner','computer_use_replay.discovery','computer_use_replay.providers'}:
   raise RuntimeError('invoke attempted to import model discovery')
sys.meta_path.insert(0,NoPlanner())
from computer_use_replay.cli import main
main()
"""
    async with serve_demo("normal") as (origin, app):
        listed = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            code,
            "catalog",
            "--capabilities",
            "capabilities",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        list_out, list_err = await asyncio.wait_for(listed.communicate(), 15)
        assert listed.returncode == 0, list_err.decode()
        catalog = json.loads(list_out)
        assert {entry["name"] for entry in catalog} == {"read_savings", "prepare_subaccount"}
        savings = next(entry for entry in catalog if entry["name"] == "read_savings")
        assert savings["parameters"]["properties"]["member_id"]["x-sensitive"] is True
        assert "00123" not in list_out.decode() and "00456" not in list_out.decode()

        invoked = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            code,
            "invoke",
            "read_savings",
            "--capabilities",
            "capabilities",
            "--target",
            origin,
            "--args",
            '{"member_id":"00456"}',
            "--evidence",
            str(tmp_path / "runs"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(invoked.communicate(), 15)
        assert invoked.returncode == 0, err.decode()
        result = json.loads(out)
        assert result["status"] == "success"
        assert result["outputs"]["available_balance"]["amount"] == "8902.10"
        assert result["llm_calls"] == 0
        assert app.state.finalizations == 0


async def test_invoke_unknown_capability_name_fails_before_browser_creation(tmp_path):
    code = """from unittest.mock import patch
from computer_use_replay.cli import main
with patch("computer_use_replay.cli.BrowserSurface", side_effect=AssertionError("Browser must not open")):
 main()
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        "invoke",
        "does_not_exist",
        "--capabilities",
        "capabilities",
        "--args",
        "{}",
        "--evidence",
        str(tmp_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await asyncio.wait_for(process.communicate(), 10)
    assert process.returncode == 2, err.decode()


async def test_catalog_accepts_a_tenant_presentation_overlay():
    code, out, err = await command(
        "catalog",
        "--capabilities",
        "capabilities",
        "--presentation",
        "profiles/tenant_b.json",
    )
    assert code == 0, err
    entries = json.loads(out)
    assert {entry["name"] for entry in entries} == {"read_savings", "prepare_subaccount"}
    # The overlay changes locators only; the declared product and outcome codes,
    # which the catalog surfaces, are unaffected by which tenant presentation loads.
    savings = next(entry for entry in entries if entry["name"] == "read_savings")
    assert savings["result_statuses"]["business_outcome"]["codes"] == [
        "member_not_found",
        "permission_denied",
        "validation_error",
    ]


async def test_cli_invalid_json_does_not_print_input_secret(tmp_path):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "computer_use_replay.cli",
        "discover",
        "--request",
        "requests/read_savings.json",
        "--goal",
        "Read savings",
        "--inputs",
        "SECRET-invalid-json",
        "--evidence",
        str(tmp_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), 15)
    assert process.returncode == 1
    assert json.loads(stdout)["failure"]["intervention_id"] is None
    assert b"SECRET-invalid-json" not in stdout + stderr


@pytest.mark.parametrize("decision_budget", [None, 0, 2])
async def test_discovery_cli_uses_http_provider_and_reports_failure(tmp_path, decision_budget):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    requests = []

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            body = json.dumps(
                {
                    "done": True,
                    "message": {"tool_calls": [{"function": {"name": "finish", "arguments": {}}}]},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async with serve_demo() as (origin, _):
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "computer_use_replay.cli",
                "discover",
                "--request",
                "requests/read_savings.json",
                "--target",
                origin,
                "--model-url",
                f"http://127.0.0.1:{server.server_port}",
                "--model",
                "explicit-test-provider",
                *(
                    []
                    if decision_budget is None
                    else ["--model-decision-retries", str(decision_budget)]
                ),
                "--goal",
                "Read savings",
                "--inputs",
                '{"member_id":"00123"}',
                "--artifact",
                str(tmp_path / "absent.json"),
                "--evidence",
                str(tmp_path / "runs"),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), 10)
        assert proc.returncode == 1, err.decode()
        assert json.loads(out)["failure"]["code"] == "invalid_model_response"
        budget = 1 if decision_budget is None else decision_budget
        assert len(requests) == budget + 1 and "00123" not in json.dumps(requests)
        assert json.loads(out)["llm_calls"] == budget + 1
        assert not (tmp_path / "absent.json").exists()
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join()


async def test_serve_cli_health_and_graceful_shutdown():
    import signal
    import socket

    import httpx

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "computer_use_replay.cli",
        "serve",
        "--port",
        str(port),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with httpx.AsyncClient() as client, asyncio.timeout(5):
            while True:
                try:
                    response = await client.get(f"http://127.0.0.1:{port}/health")
                    break
                except httpx.ConnectError:
                    await asyncio.sleep(0.05)
        assert response.json() == {"ok": True}
        proc.send_signal(signal.SIGINT)
        await asyncio.wait_for(proc.wait(), 5)
        assert proc.returncode in (0, -signal.SIGINT)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


@pytest.mark.parametrize(
    "command,extra,inputs,exit_code",
    [
        ("discover", ["--request", "requests/prepare_subaccount.json"], '{"member_id":123}', 1),
        ("replay", ["--artifact", "does-not-exist.json"], '{"member_id":"00123"}', 2),
        ("discover", ["--model-decision-retries", "3"], '{"member_id":"00123"}', 2),
    ],
)
async def test_cli_rejects_bad_arguments_before_browser_creation(
    tmp_path, command, extra, inputs, exit_code
):
    code = """from unittest.mock import patch
from computer_use_replay.cli import main
with patch("computer_use_replay.cli.BrowserSurface", side_effect=AssertionError("Browser must not open")):
 main()
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        command,
        *extra,
        "--inputs",
        inputs,
        "--evidence",
        str(tmp_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(process.communicate(), 10)
    assert process.returncode == exit_code, err.decode()
    if exit_code == 1:
        result = json.loads(out)
        assert result["failure"]["code"] == "invalid_input"
        assert result["failure"]["intervention_id"] is None
        assert result["failure"]["screenshot"] is None


async def command(*args):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "computer_use_replay.cli",
        *map(str, args),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(process.communicate(), 20)
    return process.returncode, out, err


@pytest.mark.parametrize("pattern", [r"^(a+)+$", r"^(a|aa)+$", r"^(a*)*$"])
def test_pathological_patterns_are_bounded(pattern):
    code = f"""
from computer_use_replay.contracts import Input
spec = Input(kind="identifier", pattern={pattern!r}, max_length=1000)
for size in [31, 100, 1000]:
    try:
        spec.validate_value("a" * (size - 1) + "!")
    except ValueError:
        pass
    else:
        raise AssertionError("nonmatching identifier accepted")
assert spec.validate_value("aaa") == "aaa"
"""
    result = subprocess.run([sys.executable, "-c", code], timeout=5, capture_output=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("pattern", [r"(a)\1", r"(?<=a)b", r"(?=a)a", "["])
def test_unsupported_pattern_fails_quietly(pattern, capfd):
    with pytest.raises(ValueError, match="invalid identifier pattern"):
        Input(kind="identifier", pattern=pattern)
    assert capfd.readouterr().err == ""


def test_matcher_is_not_part_of_wire_contract():
    spec = Input(kind="identifier", pattern="M-[A-Z]{2}[0-9]{3}")
    restored = Input.model_validate_json(spec.model_dump_json())
    assert restored.validate_value("M-AB123") == "M-AB123"
    assert "_identifier" not in spec.model_dump_json()


@pytest.mark.parametrize(
    "value",
    [
        " Leading",
        "Trailing ",
        "   ",
        "two  spaces",
        "line\nbreak",
        "tab\tname",
        "a\x00b",
        "a\x7fb",
        "a\x85b",
        "a\ud800b",
        "🏦" * 21,
        "x" * 41,
    ],
)
async def test_lossy_text_rejected_before_browser(tmp_path, value):
    code, out, _ = await command(
        "replay",
        "--target",
        "http://127.0.0.1:1",
        "--artifact",
        "capabilities/prepare_subaccount.json",
        "--inputs",
        json.dumps({"member_id": "00123", "nickname": value}),
        "--evidence",
        tmp_path,
    )
    assert code == 1
    result = json.loads(out)
    assert result["failure"]["code"] == "invalid_input"
    assert result["failure"]["intervention_id"] is None
    assert not list(tmp_path.glob("*/events.jsonl"))


@pytest.mark.parametrize("value", ["旅行 🏦", "🏦" * 20, "x" * 40, "<script>alert(1)</script>"])
async def test_canonical_text_survives_real_browser(live, subaccount_capability, value):
    assert Input(kind="text", max_length=40).validate_value(value) == value
    async with live() as (ex, app):
        result = await Replay(ex).run(
            subaccount_capability, {"member_id": "00123", "nickname": value}
        )
        assert result.status == "success" and result.llm_calls == 0
        assert app.state.finalizations == 0


async def test_missing_session_cannot_share_member_state():
    app = create_app()
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://fixture"
        ) as first,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://fixture"
        ) as second,
    ):
        assert (await first.post("/desk/search", data={"member_key": "00123"})).status_code == 409
        assert (await second.post("/desk/member")).status_code == 409
        assert app.state.sessions == {}


async def test_fixture_finalization_is_real_and_requires_session():
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        assert (await client.post("/desk/finalize")).status_code == 409
        assert app.state.finalizations == 0
        await client.get("/")
        await client.post("/desk/search", data={"member_key": "00123"})
        response = await client.post("/desk/finalize")
        assert "Account closed" in response.text
        assert app.state.finalizations == 1


async def test_session_renewal_persists_for_next_account_navigation():
    app = create_app("expired")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        await client.get("/")
        assert "Session expired" in (await client.post("/desk/accounts")).text
        assert "Account directory" in (await client.post("/desk/renew")).text
        assert "Account directory" in (await client.post("/desk/accounts")).text


async def test_subaccount_commit_exists_but_is_outside_automation_routes():
    from pathlib import Path

    import pytest

    from computer_use_replay.policy import Binding, Policy, Stop

    app = create_app()
    binding = Binding.load(Path("profiles/juniper.json"))
    with pytest.raises(Stop, match="network_policy"):
        Policy(binding, "http://fixture").check_url("http://fixture/desk/subaccount/create", "POST")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        await client.get("/")
        assert app.state.finalizations == 0
        assert "Sub-account created" in (await client.post("/desk/subaccount/create")).text
        assert app.state.finalizations == 1
