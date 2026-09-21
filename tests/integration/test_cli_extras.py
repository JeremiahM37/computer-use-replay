"""CLI-process coverage for --input/--input-env, --integration, and `demo`."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from computer_use_replay.demo import serve_demo


async def command(*args, env=None):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "computer_use_replay.cli",
        *map(str, args),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **(env or {})},
    )
    out, err = await asyncio.wait_for(process.communicate(), 30)
    return process.returncode, out, err


async def test_invoke_input_and_input_env_replay_without_json(tmp_path):
    async with serve_demo("normal") as (origin, _app):
        code, out, err = await command(
            "invoke",
            "read_savings",
            "--capabilities",
            "capabilities",
            "--target",
            origin,
            "--input-env",
            "member_id=DEMO_MEMBER_ID",
            "--evidence",
            tmp_path,
            env={"DEMO_MEMBER_ID": "00456"},
        )
        assert code == 0, err.decode()
        result = json.loads(out)
        assert result["outputs"]["available_balance"]["amount"] == "8902.10"
        assert "00456" not in err.decode()


async def test_invoke_fallback_off_flag_still_succeeds_when_no_rescue_is_needed(tmp_path):
    # --fallback off just overrides binding.fallback before the run; with an
    # untouched fixture there is nothing to rescue, so this only exercises the
    # CLI plumbing (cli.py's `getattr(args, "fallback", None)` override).
    async with serve_demo("normal") as (origin, _app):
        code, out, err = await command(
            "invoke",
            "read_savings",
            "--capabilities",
            "capabilities",
            "--target",
            origin,
            "--input",
            "member_id=00123",
            "--evidence",
            tmp_path,
            "--fallback",
            "off",
        )
        assert code == 0, err.decode()
        result = json.loads(out)
        assert result["outputs"]["available_balance"]["amount"] == "1204.57"


async def test_missing_input_env_variable_exits_2_and_names_the_variable(tmp_path):
    code, out, err = await command(
        "invoke",
        "read_savings",
        "--target",
        "http://127.0.0.1:1",
        "--input-env",
        "member_id=COMPUTER_USE_REPLAY_TEST_DOES_NOT_EXIST",
        "--evidence",
        tmp_path,
    )
    assert code == 2
    message = err.decode()
    assert "COMPUTER_USE_REPLAY_TEST_DOES_NOT_EXIST" in message
    assert out == b""


async def test_input_key_collision_with_inputs_json_is_invalid_input(tmp_path):
    code, out, err = await command(
        "invoke",
        "read_savings",
        "--target",
        "http://127.0.0.1:1",
        "--args",
        '{"member_id":"00123"}',
        "--input",
        "member_id=00456",
        "--evidence",
        tmp_path,
    )
    assert code == 1
    result = json.loads(out)
    assert result["failure"]["code"] == "invalid_input"


async def test_integration_flag_fills_catalog_capabilities_dir():
    code, out, err = await command("catalog", "--integration", "meridian")
    assert code == 0, err.decode()
    catalog = json.loads(out)
    assert {entry["name"] for entry in catalog} == {"read_savings"}


async def test_integration_flag_fills_catalog_capabilities_dir_for_erpnext():
    code, out, err = await command("catalog", "--integration", "erpnext")
    assert code == 0, err.decode()
    catalog = json.loads(out)
    assert {entry["name"] for entry in catalog} == {"prepare_quotation"}


def _current_and_stale_artifacts(directory):
    current = json.loads(Path("capabilities/read_savings.json").read_text())
    (directory / "current.json").write_text(json.dumps(current))
    stale = dict(current, name="stale_lookup", binding_sha256="0" * 64)
    (directory / "stale.json").write_text(json.dumps(stale))


async def test_catalog_names_a_stale_artifact_on_stderr_and_keeps_stdout_a_clean_catalog(tmp_path):
    _current_and_stale_artifacts(tmp_path)
    code, out, err = await command("catalog", "--capabilities", tmp_path)
    assert code == 0, err.decode()
    assert [entry["name"] for entry in json.loads(out)] == ["read_savings"]
    assert "not listed: stale.json (stale_lookup): binding_mismatch" in err.decode()


async def test_unknown_integration_exits_2(tmp_path):
    code, out, err = await command(
        "invoke",
        "read_savings",
        "--integration",
        "does-not-exist",
        "--target",
        "http://127.0.0.1:1",
        "--evidence",
        tmp_path,
    )
    assert code == 2
    assert out == b""


async def test_demo_replays_the_committed_tour_zero_model_calls(tmp_path):
    code, out, err = await command("demo", "--evidence", tmp_path / "demo")
    assert code == 0, err.decode()
    summary = json.loads(out)
    assert summary["ok"] is True
    assert summary["discovery"] is None
    assert [step["step"] for step in summary["steps"]] == ["a", "b", "c", "d", "e", "f", "g", "h"]
    assert all(step["model_calls"] == 0 for step in summary["steps"])
    table = err.decode()
    assert "step" in table and "model calls" in table
    # The README invites `demo`, then `demo --present`, then `demo --discover` back to
    # back: a second run into the same evidence root must not collide with the first.
    code, out, err = await command("demo", "--evidence", tmp_path / "demo")
    assert code == 0, err.decode()
    assert json.loads(out)["ok"] is True


async def test_demo_present_runs_headless_under_the_env_override(tmp_path):
    code, out, err = await command(
        "demo",
        "--present",
        "--pace",
        "0",
        "--evidence",
        tmp_path / "demo-present",
        env={"COMPUTER_USE_REPLAY_HEADLESS": "1"},
    )
    assert code == 0, err.decode()
    summary = json.loads(out)
    assert summary["ok"] is True


@pytest.mark.parametrize("value", ["00123", "00999"])
async def test_replay_present_flag_is_accepted_headless(tmp_path, value):
    async with serve_demo("normal") as (origin, _app):
        code, out, err = await command(
            "replay",
            "--target",
            origin,
            "--artifact",
            "capabilities/read_savings.json",
            "--inputs",
            json.dumps({"member_id": value}),
            "--evidence",
            tmp_path,
            "--present",
            "--pace",
            "0",
            env={"COMPUTER_USE_REPLAY_HEADLESS": "1"},
        )
        assert code == 0, err.decode()
