"""Browser lifecycle, network authority, uncertain actions, and bounded recovery."""

import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import WebSocket
from fastapi.responses import RedirectResponse
from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeout

from computer_use_replay.browser import BrowserSurface
from computer_use_replay.contracts import Capability, Condition, Fill, Input, Target
from computer_use_replay.engine import Replay
from computer_use_replay.network import RequestRule
from computer_use_replay.policy import Binding, Control, State, Stop


async def wait_block(surface, code):
    async with asyncio.timeout(2):
        while surface.blocked is None:  # noqa: ASYNC110 — adapter exposes state, not an event
            await asyncio.sleep(0.01)
    with pytest.raises(Stop, match=code):
        surface.check_health()


async def test_popup_is_closed(live):
    async with live() as (ex, _):
        await ex.surface.navigate()
        async with ex.surface.page.expect_popup() as event:
            await ex.surface.page.evaluate("window.open('about:blank')")
        popup = await event.value
        await wait_block(ex.surface, "unexpected_window")
        async with asyncio.timeout(2):
            while not popup.is_closed():  # noqa: ASYNC110 — wait for handler closure to finish
                await asyncio.sleep(0.01)
        assert len(ex.surface.context.pages) == 1


async def test_download_is_cancelled(live):
    async with live() as (ex, _):
        await ex.surface.navigate()
        async with ex.surface.page.expect_download() as event:
            await ex.surface.page.evaluate("""() => {
                const a=document.createElement('a'); a.download='private.txt';
                a.href=URL.createObjectURL(new Blob(['SENSITIVE DOWNLOAD'])); a.click();
            }""")
        download = await event.value
        await wait_block(ex.surface, "unexpected_download")
        assert await download.failure() is not None
        assert not list(ex.evidence.directory.glob("*.txt"))


async def test_websocket_never_reaches_server(live):
    async with live() as (ex, app):
        received = []

        @app.websocket("/ws")
        async def socket(ws: WebSocket):
            received.append(True)
            await ws.accept()
            await ws.close()

        await ex.surface.navigate()
        await ex.surface.page.evaluate(
            "url => new WebSocket(url)", ex.policy.origin.replace("http:", "ws:") + "/ws"
        )
        await wait_block(ex.surface, "network_policy")
        assert received == []


async def test_allowed_destination_redirect_still_not_followed(live):
    async with live() as (ex, app):
        reached = []
        # Replace the fixture entry with a redirect and trap the allowlisted destination.
        app.router.routes[:] = [r for r in app.router.routes if r.path not in {"/", "/desk/search"}]

        @app.get("/")
        async def redirect():
            return RedirectResponse("/desk/search")

        @app.get("/desk/search")
        async def destination():
            reached.append(True)
            return "must not be reached"

        with pytest.raises(Stop, match="redirect_not_allowed"):
            await ex.surface.navigate()
        assert reached == []


async def test_duplicate_frame_and_missing_target(live):
    async with live() as (ex, _):
        await ex.surface.navigate()
        await ex.surface.page.evaluate(
            "document.body.insertAdjacentHTML('beforeend','<iframe name=workbench></iframe>')"
        )
        with pytest.raises(Stop, match="ambiguous_frame"):
            await ex.surface.observe()
        await ex.surface.page.locator("iframe").last.evaluate("el=>el.remove()")
        with pytest.raises(Stop, match="target_missing"):
            await ex.surface._unique("balance")
        desk = ex.surface.page.frame(name="workbench")
        await desk.evaluate(
            "document.body.insertAdjacentHTML('beforeend','<button>Find member</button>')"
        )
        with pytest.raises(Stop, match="ambiguous_target"):
            await ex.surface.condition(Condition(target="search"), {})


@pytest.mark.parametrize(
    "fault,code", [("mismatch", "fill_mismatch"), ("timeout", "effect_uncertain")]
)
async def test_fill_faults_fail_without_retry(live, fault, code):
    async with live() as (ex, _):
        await ex.surface.navigate()
        loc = await ex.surface._unique("member_input")
        if fault == "mismatch":
            loc.input_value = AsyncMock(return_value="changed-by-app")
        else:
            loc.fill = AsyncMock(side_effect=BrowserTimeout("timeout contains SECRET"))
        ex.surface._unique = AsyncMock(return_value=loc)
        with pytest.raises(Stop, match=code):
            await ex.surface.perform(
                Fill(target="member_input", input="member_id"), {"member_id": "00123"}
            )
        if fault == "timeout":
            loc.fill.assert_awaited_once()


@pytest.mark.parametrize(
    "message,code",
    [
        ("arbitrary sensitive adapter error", "observation_failed"),
        ("Execution context was destroyed", "observation_timeout"),
    ],
)
async def test_observation_errors_are_bounded_and_sanitized(live, message, code):
    async with live() as (ex, _):
        ex.policy.binding = ex.policy.binding.model_copy(update={"step_timeout": 0.03})
        ex.surface._observe_once = AsyncMock(side_effect=BrowserError(message))
        with pytest.raises(Stop, match=code):
            await ex.surface.observe()


async def test_failed_initialization_closes_started_runtime(live):
    async with live() as (ex, _):
        surface = BrowserSurface(ex.policy, ex.handoff.ownership, ex.evidence)
        # Inject launch failure after real Playwright startup, then inspect actual cleanup.
        with patch.dict(
            "os.environ",
            {"COMPUTER_USE_REPLAY_CHROMIUM": "/nonexistent/computer-use-replay-browser"},
        ):
            with pytest.raises(BrowserError):
                await surface.__aenter__()
        assert surface.pw is not None
        assert surface.browser is None
        with pytest.raises(BrowserError, match="closed"):
            await surface.pw.chromium.launch()


async def test_record_video_dir_env_writes_a_webm_at_a_fixed_size(live, tmp_path):
    """Opt-in COMPUTER_USE_REPLAY_RECORD_VIDEO=<dir> for walkthroughs/demos: a fixed
    1280x800 viewport/recording size so the frame is stable whether the run is
    headed or headless. Off by default -- every other test in this suite never
    sets it and produces no video.
    """
    video_dir = tmp_path / "videos"
    async with live() as (ex, _):
        with patch.dict("os.environ", {"COMPUTER_USE_REPLAY_RECORD_VIDEO": str(video_dir)}):
            surface = BrowserSurface(ex.policy, ex.handoff.ownership, ex.evidence)
            async with surface:
                assert surface.page.viewport_size == {"width": 1280, "height": 800}
                assert surface.page.video is not None
                await surface.navigate()
    assert list(video_dir.glob("*.webm"))


async def test_navigation_and_transport_failures(live):
    async with live() as (ex, _):
        await ex.surface.navigate()
        ex.surface.page.goto = AsyncMock(side_effect=BrowserError("SECRET"))
        with pytest.raises(Stop, match="navigation_failed"):
            await ex.surface.navigate()
        # A real allowed request with the fetch transport seam made unavailable.
        from playwright.async_api import Route

        with patch.object(Route, "fetch", AsyncMock(side_effect=BrowserError("connection reset"))):
            await ex.surface.page.evaluate("fetch('/').catch(()=>{})")
            await wait_block(ex.surface, "load_failed")


async def test_missing_frame_never_falls_back_to_matching_parent_control(live):
    async with live() as (ex, _):
        await ex.surface.navigate()
        await ex.surface.page.evaluate("""() => {
            document.querySelector('iframe').remove();
            document.body.insertAdjacentHTML('beforeend', '<button>Find member</button>');
        }""")
        assert await ex.surface.page.get_by_role("button", name="Find member").count() == 1
        assert await ex.surface.count("search") == 0
        assert not await ex.surface.condition(Condition(target="search"), {})
        with pytest.raises(Stop, match="target_missing"):
            await ex.surface._unique("search")


async def test_adapter_rejects_unknown_step_type_instead_of_reporting_completion(live):
    from types import SimpleNamespace

    async with live() as (ex, _):
        await ex.surface.navigate()
        unknown = SimpleNamespace(op="fill", target="member_input", input="member_id")
        with pytest.raises(Stop, match="unsupported_action"):
            await ex.surface.perform(unknown, {"member_id": "00123"})
        assert "action_completed" not in (ex.evidence.directory / "events.jsonl").read_text()


async def test_navigation_generation_discards_stale_successful_snapshot(live):
    async with live() as (ex, _):
        await ex.surface.navigate()
        original = ex.surface._observe_once
        stale = await original()
        calls = 0

        async def crossed_navigation():
            nonlocal calls
            calls += 1
            if calls == 1:
                ex.surface.generation += 1
                return stale
            return await original()

        ex.surface._observe_once = crossed_navigation
        current = await ex.surface.observe()
        assert calls == 2
        assert current is not stale and ex.surface.last_snapshot is current
        assert any(node.target == "member_input" and node.count == 1 for node in current.controls)


async def test_already_settled_request_keeps_policy_failure_without_listener_crash(live):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from playwright.async_api import Error

    async with live() as (ex, _):
        route = SimpleNamespace(
            request=SimpleNamespace(url="https://blocked.invalid/", method="GET", post_data=None),
            abort=AsyncMock(side_effect=Error("Route is already handled!")),
        )
        await ex.surface._request(route)
        assert ex.surface.blocked == "network_policy"
        with pytest.raises(Stop, match="network_policy"):
            ex.surface.check_health()


@pytest.mark.parametrize("replacement", ["missing", "duplicate"])
async def test_condition_handles_target_change_between_count_and_read(live, replacement):
    async with live("normal") as (ex, _):
        await ex.surface.navigate()
        original = ex.surface._unique

        async def changed(key):
            loc = ex.surface._locator(ex.policy.binding.controls[key].target)
            if replacement == "missing":
                await loc.evaluate("el => el.remove()")
            else:
                await loc.evaluate("""el => {
                    const copy = el.cloneNode(true); copy.removeAttribute("id");
                    copy.setAttribute("aria-label", "Member identifier"); el.after(copy);
                }""")
            return await original(key)

        with patch.object(ex.surface, "_unique", side_effect=changed):
            condition = Condition(target="member_input", kind="equals_input", input="member_id")
            if replacement == "missing":
                assert not await ex.surface.condition(condition, {"member_id": "00123"})
            else:
                with pytest.raises(Stop, match="ambiguous_target"):
                    await ex.surface.condition(condition, {"member_id": "00123"})


@pytest.mark.parametrize(
    "exc,raises",
    [
        (BrowserTimeout("stale locator: SECRET context, timeout 3000ms exceeded"), False),
        (BrowserError("Locator.evaluate: Execution context was destroyed, SECRET"), False),
        (BrowserError("Locator.evaluate: Frame was detached, SECRET"), False),
        (BrowserError("SECRET arbitrary adapter fault"), True),
    ],
)
async def test_condition_read_tolerates_navigation_races_not_other_faults(live, exc, raises):
    # count() can observe a control an instant before a concurrent navigation invalidates
    # it; reading it then either times out on the stale locator or loses its execution
    # context outright. Both are "not yet satisfied", not an adapter fault -- the caller's
    # own bounded retry loop re-observes. An unrelated browser error still propagates.
    async with live("normal") as (ex, _):
        await ex.surface.navigate()
        condition = Condition(target="member_input", kind="equals_input", input="member_id")
        with patch.object(ex.surface, "_text", AsyncMock(side_effect=exc)):
            if raises:
                with pytest.raises(type(exc), match="SECRET"):
                    await ex.surface.condition(condition, {"member_id": "00123"})
            else:
                assert not await ex.surface.condition(condition, {"member_id": "00123"})


@pytest.mark.parametrize(
    "kind,code",
    [
        ("input", "invalid_input"),
        ("timeout", "run_timeout"),
        ("adapter", "surface_error"),
        ("fill", "fill_mismatch"),
    ],
)
async def test_replay_faults_are_structured(live, capability, kind, code):
    async with live() as (ex, app):
        if kind == "timeout":
            ex.policy.binding = ex.policy.binding.model_copy(update={"run_timeout": 0.02})
            capability = capability.model_copy(
                update={"binding_sha256": ex.policy.binding.digest()}
            )

            async def delayed():
                await asyncio.sleep(1)

            ex.surface.navigate = delayed
        if kind == "adapter":
            ex.surface.navigate = AsyncMock(side_effect=RuntimeError("PRIVATE raw adapter message"))
        if kind == "fill":
            ex.surface.perform = AsyncMock(side_effect=Stop("fill_mismatch"))
        result = await Replay(ex).run(
            capability, {"member_id": 123 if kind == "input" else "00123"}
        )
        assert result.failure.code == code
        assert "PRIVATE" not in result.model_dump_json()
        assert app.state.finalizations == 0


@pytest.mark.parametrize(
    "case,code",
    [
        ("budget", "recovery_exhausted"),
        ("no_effect", "recovery_effect_uncertain"),
        ("transient", "load_timeout"),
    ],
)
async def test_recovery_is_bounded(live, case, code):
    async with live() as (ex, _):
        await ex.surface.navigate()
        # Actual rendered UI with a declared state that never clears.
        desk = ex.surface.page.frame(name="workbench")
        html = (
            "<h2>Loading accounts</h2>"
            if case == "transient"
            else "<h2>Service notice</h2><button>Dismiss service notice</button>"
        )
        await desk.set_content(html)
        ex.policy.binding = ex.policy.binding.model_copy(
            # Allow real browser observation time under coverage/parallel host load.
            # The state must still hit its exact engine timeout, not an adapter timeout.
            update={"step_timeout": 1.0, "max_recoveries": 0 if case == "budget" else 2}
        )
        with pytest.raises(Stop, match=code):
            await ex.settle(Condition(target="accounts_screen"))
        assert ex.recoveries == (0 if case == "transient" else 1)


async def test_successful_interstitial_recovery_gets_a_fresh_deadline(live):
    """The dialog and human branches in settle() reset `deadline` after handling;
    the interstitial branch did not -- so a recovery that legitimately used most
    of the step window left the caller almost no time to observe the condition it
    was actually waiting on, and a genuinely successful recovery was immediately
    followed by a spurious failure. max_recoveries still bounds total recovery
    work; this only restores the window for what happens AFTER recovery succeeds.

    Real wall-clock timing (small, real delays) against the real browser/demo app
    -- nothing here is mocked. The recovery (dismissing the spinner) is made to
    take close to the whole step_timeout, on purpose: that is what proves the
    fix, since a recovery that finishes instantly wouldn't tell buggy and fixed
    code apart.
    """
    async with live() as (ex, _):
        await ex.surface.navigate()
        desk = ex.surface.page.frame(name="workbench")
        await desk.set_content(
            '<h2 id="spinner">Loading</h2>'
            '<button id="dismiss" onclick="setTimeout(()=>'
            "document.getElementById('spinner').remove(), 1500)\">Dismiss</button>"
            '<div id="result">wrong-value</div>'
        )
        controls = {
            "spinner": Control(
                target=Target(kind="css", name="#spinner", frames=("workbench",)),
                description="Loading spinner",
            ),
            "dismiss": Control(
                target=Target(kind="css", name="#dismiss", frames=("workbench",)),
                operations=("click",),
                description="Dismiss control",
            ),
            "result": Control(
                target=Target(kind="css", name="#result", frames=("workbench",)),
                description="The condition settle() is actually waiting on",
            ),
        }
        ex.policy.binding = Binding(
            product=ex.policy.binding.product,
            entry="/",
            routes={"/": ("GET",)},
            controls=controls,
            states={"loading": State(target="spinner", kind="interstitial", recovery="dismiss")},
            input_types={"expected": Input(kind="text")},
            invariants=(Condition(target="result", kind="visible"),),
            step_timeout=3.0,
        )
        condition = Condition(target="result", kind="equals_input", input="expected")
        started = time.monotonic()
        with pytest.raises(Stop) as excinfo:
            await ex.settle(condition, {"expected": "right-value"}, step=0)
        elapsed = time.monotonic() - started

        # The interstitial genuinely, successfully cleared -- recovery worked,
        # well within its recovery budget (max_recoveries defaults to 2).
        assert ex.recoveries == 1
        # "result" never equals "right-value", so the real awaited condition is
        # never satisfied -- either target_drift or checkpoint_failed is a valid
        # outcome here (both come from the same bottom-of-loop deadline check);
        # what this test is actually about is how much time elapsed before it.
        assert excinfo.value.code in {"checkpoint_failed", "target_drift"}
        # BUGGY code: deadline is never reset after the interstitial resolves, so
        # settle() fails within roughly one step_timeout total (~3s) no matter how
        # much of that window the recovery itself consumed.
        # FIXED code: the successful recovery earns a fresh step_timeout window for
        # "result" (which never matches), so total elapsed is the 1.5s dismiss delay
        # plus a full window (~4.5s). The windows are deliberately generous so the
        # recovery itself always lands inside its budget on a slow CI runner.
        assert elapsed >= 3.0 * 1.3, (
            f"only {elapsed:.3f}s elapsed -- the recovery did not get a fresh deadline"
        )


async def test_uncertain_click_is_checked_never_repeated(live, capability):
    async with live() as (ex, _):
        perform = ex.surface.perform
        calls = []

        async def uncertain(step, arguments):
            value = await perform(step, arguments)
            calls.append(step.target)
            if step.target == "search":
                raise Stop("effect_uncertain")
            return value

        ex.surface.perform = uncertain
        result = await Replay(ex).run(capability, {"member_id": "00123"})
        assert result.status == "success", result
        assert calls.count("search") == 1
        assert "effect_confirmed" in (ex.evidence.directory / "events.jsonl").read_text()


async def test_checkpoint_repair_resumes_without_repeating_action(live, capability):
    async with live("wrong_member") as (ex, _):

        async def operator(owner, request, validate):
            lease = await owner.claim(request)
            # Test operator repairs the displayed identity in the live frame.
            # Not a real application action and never represented as genuine human evidence.
            await (
                ex.surface.page.frame(name="workbench")
                .locator("tr")
                .filter(has_text="Member identifier")
                .locator("td")
                .evaluate("el=>el.textContent='00123'")
            )
            await owner.resume(lease, validate)

        ex.handoff.operator = operator
        result = await Replay(ex).run(capability, {"member_id": "00123"})
        # Final checkpoints must use the same handoff mechanism as step receipts.
        assert result.status == "success", result
        rows = [
            json.loads(x) for x in (ex.evidence.directory / "events.jsonl").read_text().splitlines()
        ]
        assert (
            sum(x["event"] == "action_started" and x.get("target") == "savings" for x in rows) == 1
        )


async def test_policy_tampering_stops_before_entry_navigation(live, capability):
    raw = capability.model_dump(mode="json")
    raw["steps"][1]["target"] = "finalize"
    async with live() as (ex, app):
        result = await Replay(ex).run(Capability.model_validate(raw), {"member_id": "00123"})
        assert result.failure.code == "human_required"
        assert app.state.finalizations == 0 and not app.state.sessions


async def test_unknown_dom_dialog_is_not_blindly_dismissed(live, capability):
    async with live() as (ex, _):
        navigate = ex.surface.navigate

        async def injected():
            await navigate()
            await ex.surface.page.frame(name="workbench").evaluate(
                "document.body.insertAdjacentHTML('afterbegin','<div role=dialog>Unrecognized prompt<button>Confirm</button></div>')"
            )

        ex.surface.navigate = injected
        result = await Replay(ex).run(capability, {"member_id": "00123"})
        assert result.failure.code == "operator_unavailable"
        assert await ex.surface.page.frame(name="workbench").get_by_role("dialog").is_visible()


async def test_closed_browser_is_reported_without_raw_exception(live, capability):
    async with live() as (ex, _):
        await ex.surface.page.close()
        result = await Replay(ex).run(capability, {"member_id": "00123"})
        assert result.failure.code == "session_closed"
        assert "TargetClosedError" not in result.model_dump_json()


async def test_browser_isolation_between_runs(live, capability):
    async with live() as (first, _):
        result = await Replay(first).run(capability, {"member_id": "00123"})
        assert result.status == "success"
        cookie_a = await first.surface.context.cookies()
    async with live() as (second, _):
        result = await Replay(second).run(capability, {"member_id": "00456"})
        assert result.status == "success"
        cookie_b = await second.surface.context.cookies()
    assert cookie_a[0]["value"] != cookie_b[0]["value"]


async def test_absent_frame_is_a_structured_failure(live, capability):
    async with live() as (ex, _):
        navigate = ex.surface.navigate

        async def remove_frame():
            await navigate()
            await ex.surface.page.locator("iframe").evaluate("el=>el.remove()")

        ex.surface.navigate = remove_frame
        result = await Replay(ex).run(capability, {"member_id": "00123"})
        assert result.failure.code == "frame_missing"


async def test_cancellation_does_not_leave_automation_owning_session(live):
    async with live("expired") as (ex, _):
        claimed = asyncio.Event()

        async def waiting(owner, request, validate):
            await owner.claim(request)
            claimed.set()
            await asyncio.Event().wait()

        ex.handoff.operator = waiting
        task = asyncio.create_task(ex.handoff.intervene("session_expired", 1, ex.resumable))
        async with asyncio.timeout(2):
            await claimed.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ex.handoff.ownership.owner == "closed"


async def test_operator_time_does_not_consume_unknown_dialog_settle_budget(live, capability):
    async with live() as (ex, _):
        navigate = ex.surface.navigate

        async def inject():
            await navigate()
            await ex.surface.page.frame(name="workbench").evaluate(
                "document.body.insertAdjacentHTML('afterbegin',`<div role='dialog'><button onclick='this.parentElement.remove()'>Resolve unexpected notice</button></div>`)"
            )

        ex.surface.navigate = inject

        async def operator(owner, request, validate):
            lease = await owner.claim(request)
            # A real human commonly takes longer than the 3-second surface wait budget.
            await asyncio.sleep(ex.policy.binding.step_timeout + 0.1)
            await (
                ex.surface.page.frame(name="workbench")
                .get_by_role("button", name="Resolve unexpected notice", exact=True)
                .click()
            )
            await owner.resume(lease, validate)

        ex.handoff.operator = operator
        result = await Replay(ex).run(capability, {"member_id": "00123"})
        assert result.status == "success", result


async def test_rpc_body_constraint_blocks_dispatch_before_server(live):
    async with live() as (ex, app):
        calls = []

        @app.post("/rpc")
        async def rpc():
            calls.append("received")
            return {"ok": True}

        ex.policy.binding = ex.policy.binding.model_copy(
            update={
                "request_rules": (
                    RequestRule(
                        path="/rpc",
                        methods=("POST",),
                        body_keys=("method",),
                        body_equals={"method": "calculate"},
                    ),
                )
            }
        )
        await ex.surface.navigate()
        send = """async method => {
          try {
            const result = await fetch('/rpc', {method:'POST',
              headers:{'Content-Type':'application/x-www-form-urlencoded'},
              body:new URLSearchParams({method})});
            return await result.json();
          } catch (_) { return 'blocked'; }
        }"""
        assert await ex.surface.page.evaluate(send, "calculate") == {"ok": True}
        ex.surface.check_health()
        assert await ex.surface.page.evaluate(send, "submit") == "blocked"
        assert calls == ["received"]
        with pytest.raises(Stop, match="network_policy"):
            ex.surface.check_health()


async def test_explicit_background_discard_never_sends_http_or_websocket(live):
    async with live() as (ex, app):
        calls = []

        @app.get("/optional")
        async def optional():
            calls.append("received")
            return {}

        ex.policy.binding = ex.policy.binding.model_copy(
            update={"request_rules": (RequestRule(path="/optional", discard=True),)}
        )
        await ex.surface.navigate()
        assert await ex.surface.page.evaluate("fetch('/optional').then(()=>false).catch(()=>true)")
        await ex.surface.page.evaluate(
            "window.optionalSocket = new WebSocket(location.origin.replace('http','ws')+'/optional')"
        )
        await ex.surface.page.wait_for_function(
            "window.optionalSocket.readyState === WebSocket.CLOSED"
        )
        assert calls == []
        ex.surface.check_health()
