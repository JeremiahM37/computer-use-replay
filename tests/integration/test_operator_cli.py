"""Terminal (--human) CLI handoff and console input boundaries."""

import asyncio
import os
import sys
from contextlib import contextmanager
from unittest.mock import AsyncMock

import pytest

from computer_use_replay.cli import terminal_operator
from computer_use_replay.console import console_input
from computer_use_replay.control import Ownership
from computer_use_replay.evidence import Evidence
from computer_use_replay.policy import Stop


@contextmanager
def stdin_pipe(monkeypatch, payload=b""):
    read, write = os.pipe()
    with os.fdopen(read, "r") as stream:
        monkeypatch.setattr(sys, "stdin", stream)
        os.write(write, payload)
        try:
            yield write
        finally:
            os.close(write)


async def test_timeout_exits_process_with_stdin_open():
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import asyncio\nfrom computer_use_replay.console import console_input\n"
        "async def main():\n try:\n  async with asyncio.timeout(.05): await console_input()\n"
        ' except TimeoutError: print("timed out")\nasyncio.run(main())',
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        await asyncio.wait_for(proc.wait(), 3)
        assert proc.returncode == 0
        assert await proc.stdout.read() == b"timed out\n"
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        proc.stdin.close()


async def test_cancelled_reader_does_not_steal_next_line(monkeypatch):
    with stdin_pipe(monkeypatch) as write:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.01):
                await console_input()
        os.write(write, b"resume\ncancel\n")
        assert await console_input() == "resume"
        assert await console_input() == "cancel"


async def test_eof(monkeypatch):
    read, write = os.pipe()
    os.close(write)
    with os.fdopen(read) as stream:
        monkeypatch.setattr(sys, "stdin", stream)
        with pytest.raises(Stop, match="operator_disconnected"):
            await console_input()


async def test_input_limit(monkeypatch):
    with stdin_pipe(monkeypatch, b"x" * 257):
        with pytest.raises(Stop, match="operator_input_too_long"):
            await console_input()


async def test_unavailable_console(monkeypatch, tmp_path):
    with (tmp_path / "input").open("w+") as stream:
        monkeypatch.setattr(sys, "stdin", stream)
        with pytest.raises(Stop, match="console_unavailable"):
            await console_input()


@pytest.mark.parametrize("action", ["resume", "cancel", "stale"])
async def test_terminal_operator(monkeypatch, tmp_path, capsys, action):
    owner = Ownership(Evidence(tmp_path))
    request = await owner.pause("session_expired", 2)
    validate = AsyncMock(side_effect=[False, True])
    if action == "stale":
        validate.side_effect = Stop("stale_lease")
    payload = (
        b"unknown\nresume\nresume\n"
        if action == "resume"
        else (action if action == "cancel" else "resume").encode() + b"\n"
    )
    with stdin_pipe(monkeypatch, payload):
        if action == "resume":
            await terminal_operator(owner, request, validate)
            assert owner.owner == "automation"
            assert validate.await_count == 2
        else:
            with pytest.raises(
                Stop, match="operator_cancelled" if action == "cancel" else "stale_lease"
            ):
                await terminal_operator(owner, request, validate)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "automation paused" in captured.err
    if action == "resume":
        assert "still blocked" in captured.err
