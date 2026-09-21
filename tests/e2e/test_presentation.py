"""--present: overlay/caption/pacing, and that it never changes targeting or results.

The overlay lives only in the top-level document, is `pointer-events:none` and
`aria-hidden`, carries no role and no reviewed control's accessible name, is
removed before a failure screenshot, and is excluded from observation. This
file proves all of that, plus that captions never leak a fill VALUE (only the
input's NAME) or any balance/member text.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock

from playwright.async_api import Error as BrowserError

from computer_use_replay.browser import BrowserSurface
from computer_use_replay.contracts import Capability, Click, Condition, Fill, Input, Target
from computer_use_replay.control import Handoff, Ownership
from computer_use_replay.demo import serve_demo
from computer_use_replay.discovery import discover
from computer_use_replay.engine import Execution, Replay
from computer_use_replay.evidence import Evidence
from computer_use_replay.policy import Binding, Control, Policy
from tests.support.planner import TASK, ScriptedPlanner, decisions


async def _events(ex):
    rows = [
        json.loads(line)
        for line in (ex.evidence.directory / "events.jsonl").read_text().splitlines()
    ]
    return [{k: v for k, v in row.items() if k != "time"} for row in rows]


async def test_present_replay_is_identical_to_plain_replay(live, capability):
    async with live(pace=0) as (ex1, _app1):
        result1 = await Replay(ex1).run(capability, {"member_id": "00123"})
        snapshot1 = await ex1.surface.observe()
    async with live(present=True, pace=0) as (ex2, _app2):
        result2 = await Replay(ex2).run(capability, {"member_id": "00123"})
        snapshot2 = await ex2.surface.observe()
    assert result1.model_dump() == result2.model_dump()
    assert snapshot1.model_dump() == snapshot2.model_dump()
    assert snapshot1.unknown_dialogs == snapshot2.unknown_dialogs == 0
    assert await _events(ex1) == await _events(ex2)


async def test_present_before_action_shows_capability_step_label_and_param_name(live):
    async with live(present=True, pace=0.02) as (ex, _app):
        ex.surface.presentation_context("read_savings", 6)
        ex.surface.presentation_step(0)
        ex.surface.bind_arguments({"member_id": "00123"})
        await ex.surface.navigate()
        loc = await ex.surface._unique("member_input")
        await ex.surface._present_before(Fill(target="member_input", input="member_id"), loc)
        caption = await ex.surface.page.evaluate(
            "() => document.getElementById('__computer_use_replay_present_cap').textContent"
        )
        assert "read_savings" in caption
        assert "step 1/6" in caption
        assert "fill" in caption
        assert "Member identifier" in caption
        assert "param: member_id" in caption
        # The parameter NAME appears; the VALUE never does.
        assert "00123" not in caption
        highlight_visible = await ex.surface.page.evaluate(
            "() => document.getElementById('__computer_use_replay_present_hl').style.display"
        )
        assert highlight_visible == "block"
        # A caption-only update must clear the box: after a click navigates, nothing
        # may stay outlined where the previous control used to be.
        await ex.surface.present_outcome("verified: Search results")
        assert (
            await ex.surface.page.evaluate(
                "() => document.getElementById('__computer_use_replay_present_hl').style.display"
            )
            == "none"
        )
        overlay_role = await ex.surface.page.evaluate(
            "() => document.getElementById('__computer_use_replay_present').getAttribute('role')"
        )
        assert overlay_role is None
        aria_hidden = await ex.surface.page.evaluate(
            "() => document.getElementById('__computer_use_replay_present').getAttribute('aria-hidden')"
        )
        assert aria_hidden == "true"
        pointer_events = await ex.surface.page.evaluate(
            "() => getComputedStyle(document.getElementById('__computer_use_replay_present')).pointerEvents"
        )
        assert pointer_events == "none"


async def _recording_outcomes(surface):
    """Capture every present_outcome() caption text, still rendering each for real."""
    captions = []
    original = surface.present_outcome

    async def recording(text):
        captions.append(text)
        await original(text)

    surface.present_outcome = recording
    return captions


async def test_present_click_shows_verified_postcondition_label(live, capability):
    async with live(present=True, pace=0.02) as (ex, _app):
        captions = await _recording_outcomes(ex.surface)
        result = await Replay(ex).run(capability, {"member_id": "00123"})
        assert result.status == "success", result
        assert any(c.startswith("verified: ") for c in captions)


async def test_present_business_outcome_caption(live, capability):
    async with live(present=True, pace=0.02) as (ex, _app):
        result = await Replay(ex).run(capability, {"member_id": "00999"})
        assert result.status == "business_outcome"
        caption = await ex.surface.page.evaluate(
            "() => document.getElementById('__computer_use_replay_present_cap').textContent"
        )
        assert "business outcome: member_not_found" in caption


async def test_present_human_paused_and_resumed_captions(tmp_path, capability):
    from computer_use_replay.tour import session

    async with session(
        tmp_path, "present_human", "expired", human=True, present=True, pace=0.02
    ) as (ex, _app):
        captions = await _recording_outcomes(ex.surface)
        result = await Replay(ex).run(capability, {"member_id": "00123"})
        assert result.status == "success", result
        assert any(c.startswith("paused: session_expired") for c in captions)
        assert "operator (scripted) renewed the session" in captions
        events = await _events(ex)
        assert any(row["event"] == "intervention_requested" for row in events)
        assert any(row["event"] == "control_resumed" for row in events)


async def test_present_shows_presentation_drift_caption(tmp_path):
    # The exact committed submission artifact against tenant B's overlay, like
    # test_committed_read_savings_replays_on_tenant_b_with_one_drift_event.
    savings = Capability.model_validate_json(
        Path("capabilities/read_savings.json").read_text()  # noqa: ASYNC240 — see test_replay.py
    )
    binding = Binding.load(Path("profiles/juniper.json"))
    overlay = binding.overlay(Path("profiles/tenant_b.json"))
    async with serve_demo("tenant_b") as (origin, app):
        evidence = Evidence(tmp_path)
        owner = Ownership(evidence)
        policy = Policy(overlay, origin)
        async with BrowserSurface(policy, owner, evidence, present=True, pace=0.02) as surface:
            captions = await _recording_outcomes(surface)
            result = await Replay(Execution(surface, policy, evidence, Handoff(owner))).run(
                savings, {"member_id": "00456"}
            )
            assert result.status == "success", result
            assert app.state.finalizations == 0
    assert (
        "presentation drift: 11 reviewed hints differ — replay uses the current presentation"
        in captions
    )


async def test_present_effect_uncertain_retry_is_confirmed_not_repeated(live, capability):
    async with live(present=True, pace=0.02) as (ex, _app):
        perform = ex.surface.perform
        calls = []

        async def uncertain(step, arguments):
            value = await perform(step, arguments)
            calls.append(step.target)
            if step.target == "search":
                from computer_use_replay.policy import Stop

                raise Stop("effect_uncertain")
            return value

        ex.surface.perform = uncertain
        result = await Replay(ex).run(capability, {"member_id": "00123"})
        assert result.status == "success", result
        assert calls.count("search") == 1
        events = await _events(ex)
        assert any(
            row["event"] == "recovery" and row["code"] == "effect_confirmed" for row in events
        )


async def test_present_discovery_shows_model_chose_caption(live, capability):
    planner = ScriptedPlanner(decisions(capability))
    async with live(present=True, pace=0.02) as (ex, _app):
        artifact, result = await discover(
            ex, planner, TASK, {"member_id": "00123"}, Path("/tmp") / "unused.json"
        )
        assert result.status == "success", result
        caption = await ex.surface.page.evaluate(
            "() => document.getElementById('__computer_use_replay_present_cap').textContent"
        )
        assert "model chose:" in caption or "verified:" in caption or "read_savings" in caption


async def test_present_discovery_decision_without_a_target_skips_the_note(live):
    from computer_use_replay.planner import Decision

    async with live(present=True, pace=0.02) as (ex, _app):
        # "stop" carries no target: the presentation hook must not try to label
        # a nonexistent control, and must simply skip straight to the next check.
        artifact, result = await discover(
            ex,
            ScriptedPlanner([Decision(op="stop", reason="cannot_proceed")]),
            TASK,
            {"member_id": "00123"},
            Path("/tmp") / "unused-stop.json",
        )
        assert artifact is None
        assert result.status == "failure" and result.failure.code == "model_stuck"


async def test_failure_screenshot_removes_the_overlay_first(live, capability):
    async with live("expired", present=True, pace=0.02) as (ex, _app):
        result = await Replay(ex).run(capability, {"member_id": "00123"})
        assert result.status == "failure"
        assert result.failure.screenshot
        present = await ex.surface.page.evaluate(
            "() => !!document.getElementById('__computer_use_replay_present')"
        )
        assert present is False


async def test_overlay_evaluate_failure_is_swallowed(live):
    async with live(present=True, pace=0) as (ex, _app):
        await ex.surface.navigate()
        ex.surface.page.evaluate = AsyncMock(side_effect=BrowserError("navigated away"))
        await ex.surface.present_outcome("verified: Savings ledger")  # must not raise


async def test_present_outcome_is_a_noop_off_presentation_mode(live):
    async with live(present=False) as (ex, _app):
        await ex.surface.navigate()
        await ex.surface.present_outcome("verified: Savings ledger")  # no-op, must not raise
        overlay_present = await ex.surface.page.evaluate(
            "() => !!document.getElementById('__computer_use_replay_present')"
        )
        assert overlay_present is False


async def test_overlay_hide_failure_is_swallowed(live):
    async with live(present=True, pace=0) as (ex, _app):
        await ex.surface.navigate()
        ex.surface.page.evaluate = AsyncMock(side_effect=BrowserError("navigated away"))
        await ex.surface._overlay_hide()  # must not raise


async def test_present_before_bounding_box_failure_is_swallowed(live):
    async with live(present=True, pace=0) as (ex, _app):
        await ex.surface.navigate()
        loc = await ex.surface._unique("member_input")
        loc.bounding_box = AsyncMock(side_effect=BrowserError("gone"))
        # Must not raise, and the caption still updates from the (box=None) fallback.
        await ex.surface._present_before(Fill(target="member_input", input="member_id"), loc)
        caption = await ex.surface.page.evaluate(
            "() => document.getElementById('__computer_use_replay_present_cap').textContent"
        )
        assert "member_input" not in caption or "Member identifier" in caption


async def test_present_fill_with_after_fill_shows_verified_caption(tmp_path):
    binding = Binding(
        product="test",
        entry="/",
        routes={"/": ("GET",)},
        controls={
            "field": Control(
                target=Target(kind="css", name="input"),
                description="Search",
                operations=("fill",),
                allowed_inputs=("query",),
                after_fill=Condition(target="choice"),
            ),
            "choice": Control(
                target=Target(kind="css", name="#choices button"),
                description="Matching result",
                operations=("click",),
            ),
        },
        states={},
        input_types={"query": Input(kind="text")},
        invariants=(Condition(target="field"),),
    )
    evidence = Evidence(tmp_path)
    policy = Policy(binding, "http://localhost")
    ownership = Ownership(evidence)
    async with BrowserSurface(policy, ownership, evidence, present=True, pace=0.01) as surface:
        await surface.page.set_content('<input /><div id="choices"><button>Alpha</button></div>')
        surface.bind_arguments({"query": "Alpha"})
        execution = Execution(surface, policy, evidence, Handoff(ownership, None))
        captions = await _recording_outcomes(surface)
        await execution.step(Fill(target="field", input="query"), {"query": "Alpha"}, 0)
        assert any(c.startswith("verified: #choices button") for c in captions)


async def test_headed_present_launch_is_forced_headless_by_env(monkeypatch, tmp_path, binding):
    from computer_use_replay.browser import BrowserSurface
    from computer_use_replay.control import Ownership
    from computer_use_replay.demo import serve_demo
    from computer_use_replay.evidence import Evidence
    from computer_use_replay.policy import Policy

    monkeypatch.setenv("COMPUTER_USE_REPLAY_HEADLESS", "1")
    async with serve_demo() as (origin, _app):
        evidence = Evidence(tmp_path)
        policy = Policy(binding, origin)
        ownership = Ownership(evidence)
        async with BrowserSurface(
            policy, ownership, evidence, headed=True, present=True
        ) as surface:
            assert surface.browser.is_connected()


async def test_highlight_is_dropped_as_soon_as_a_click_is_dispatched(live):
    """A click may navigate: the box must not stay where the control used to be while
    the next screen loads -- only the caption remains until the effect is verified."""
    hl = "() => document.getElementById('__computer_use_replay_present_hl').style.display"
    cap = "() => document.getElementById('__computer_use_replay_present_cap').textContent"
    async with live(present=True, pace=0.02) as (ex, _app):
        ex.surface.presentation_context("read_savings", 6)
        ex.surface.bind_arguments({"member_id": "00123"})
        await ex.surface.navigate()
        await ex.surface.perform(
            Fill(target="member_input", input="member_id"), {"member_id": "00123"}
        )
        assert await ex.surface.page.evaluate(hl) == "block"
        click = Click(target="search", after=Condition(target="results_screen"))
        await ex.surface.perform(click, {"member_id": "00123"})
        assert await ex.surface.page.evaluate(hl) == "none"
        assert "click" in await ex.surface.page.evaluate(cap)
