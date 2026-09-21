"""Exclusive ownership, handoff leases, cancellation, and resource cleanup."""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from computer_use_replay.browser import BrowserSurface
from computer_use_replay.control import Handoff, Ownership
from computer_use_replay.demo import create_app, serve_demo
from computer_use_replay.evidence import Event, Evidence
from computer_use_replay.policy import Stop


async def yes():
    return True


async def no():
    return False


@pytest.fixture
def owner(tmp_path):
    return Ownership(Evidence(tmp_path))


async def test_competing_claims_have_one_winner(owner):
    request = await owner.pause("session_expired", 2)
    results = await asyncio.gather(
        owner.claim(request), owner.claim(request), return_exceptions=True
    )
    assert sum(isinstance(x, Stop) for x in results) == 1
    assert owner.owner == "human"


async def test_stale_lease_cannot_resume_later_intervention(owner):
    old = await owner.claim(await owner.pause("session_expired", 2))
    await owner.resume(old, yes)
    current = await owner.claim(await owner.pause("session_expired", 4))
    with pytest.raises(Stop, match="stale_lease"):
        await owner.resume(old, yes)
    await owner.resume(current, yes)


async def test_pause_waits_for_in_flight_action(owner):
    await owner.lock.acquire()
    task = asyncio.create_task(owner.pause("session_expired", 2))
    await asyncio.sleep(0)
    assert not task.done()
    assert owner.owner == "automation"
    owner.lock.release()
    await task
    assert owner.owner == "paused"


async def test_resume_revalidates_before_ownership_change(owner):
    lease = await owner.claim(await owner.pause("session_expired", 2))
    with pytest.raises(Stop, match="resume_condition_unmet"):
        await owner.resume(lease, no)
    assert owner.owner == "human"


async def test_callback_return_is_not_approval(owner):
    async def incomplete(owner, request, validate):
        await owner.claim(request)

    with pytest.raises(Stop, match="resume_condition_unmet"):
        await Handoff(owner, incomplete).intervene("session_expired", 2, yes)
    assert owner.owner == "closed"


async def test_operator_timeout_and_cancellation_close_ownership(owner):
    async def stalled(owner, request, validate):
        await owner.claim(request)
        await asyncio.Event().wait()

    with pytest.raises(TimeoutError):
        await Handoff(owner, stalled, timeout=0.02).intervene("session_expired", 2, yes)
    assert owner.owner == "closed"


async def test_missing_operator_routes_request(owner):
    with pytest.raises(Stop, match="operator_unavailable"):
        await Handoff(owner).intervene("session_expired", 2, yes)
    assert owner.owner == "paused"
    assert owner.intervention_id


def test_raw_fields_cannot_enter_event_log():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Event(event="decision", raw_page="PRIVATE")


async def test_server_startup_failure_closes_bound_socket():
    import socket

    sock = socket.socket()
    with (
        patch("computer_use_replay.demo.socket.socket", return_value=sock),
        patch(
            "computer_use_replay.demo.uvicorn.Server.serve",
            AsyncMock(side_effect=RuntimeError("startup fault")),
        ),
    ):
        with pytest.raises(RuntimeError, match="startup fault"):
            async with serve_demo():
                pytest.fail("startup failure cannot yield a running app")
    assert sock.fileno() == -1


async def test_context_cleanup_failure_still_closes_browser_and_runtime():
    surface = BrowserSurface(None, None, None)
    surface.context = Mock(close=AsyncMock(side_effect=RuntimeError("context close fault")))
    surface.browser = Mock(close=AsyncMock())
    surface.pw = Mock(stop=AsyncMock())
    with pytest.raises(RuntimeError, match="context close fault"):
        await surface.__aexit__()
    surface.browser.close.assert_awaited_once()
    surface.pw.stop.assert_awaited_once()


def test_invalid_fixture_scenario_is_rejected():
    with pytest.raises(ValueError, match="unknown demo scenario"):
        create_app("not-a-scenario")


async def test_cleanup_before_start_is_safe():
    await BrowserSurface(None, None, None).__aexit__()
