"""Discovery decisions, progress budgets, and verified artifact construction."""

import json
from pathlib import Path

import pytest

from computer_use_replay.contracts import GoalRequest
from computer_use_replay.discovery import discover
from computer_use_replay.engine import Replay
from computer_use_replay.planner import Decision
from tests.support.planner import ScriptedPlanner, decisions

TASK = GoalRequest.load(Path("requests/read_savings.json"))


async def test_discovery_serialization_and_independent_replay(live, capability, tmp_path):
    planner = ScriptedPlanner(decisions(capability))
    async with live() as (ex, _):
        path = tmp_path / "capability.json"
        artifact, result = await discover(ex, planner, TASK, {"member_id": "00123"}, path)
        assert result.status == "success", result
        assert artifact.provenance.mode == "test_fixture"
        assert planner.last_context["capability"] == TASK.name
        assert (
            planner.last_context["catalog"]["balance"]["description"]
            == ex.policy.binding.controls["balance"].description
        )
        assert planner.last_context["checkpoint"] == [
            c.model_dump(exclude_none=True) for c in TASK.checkpoint
        ]
        assert "00123" not in path.read_text()
        assert "1204.57" not in json.dumps(planner.last_context)
        assert "PRIVATE-NOTE" not in json.dumps(planner.last_context)
    async with live() as (ex, _):
        result = await Replay(ex).run(artifact, {"member_id": "00456"})
        assert result.status == "success", result
        assert result.outputs["available_balance"]["amount"] == "8902.10"


async def test_finish_rechecks_checkpoint_after_temporary_loss(live, capability, tmp_path):
    async with live() as (ex, _):
        original_condition = ex.surface.condition
        original_step = ex.step
        lost_once = False

        async def step(action, arguments, index):
            nonlocal lost_once
            value = await original_step(action, arguments, index)
            if action.op == "read":
                lost_once = True
            return value

        async def condition(check, arguments):
            nonlocal lost_once
            if lost_once:
                lost_once = False
                return False
            return await original_condition(check, arguments)

        ex.step = step
        ex.surface.condition = condition
        planner = ScriptedPlanner(decisions(capability))
        artifact, result = await discover(
            ex, planner, TASK, {"member_id": "00123"}, tmp_path / "recovered.json"
        )
        assert result.status == "success", result
        assert artifact is not None
        assert planner.last_context["collected_outputs"] == ["available_balance"]


@pytest.mark.parametrize(
    "decision,code",
    [
        (Decision(op="done", reason="goal_complete"), "false_completion"),
        (
            Decision(
                op="click", target="savings", after="savings_screen", reason="follow_navigation"
            ),
            "ungrounded_target",
        ),
        (Decision(op="stop", reason="cannot_proceed"), "model_stuck"),
        (
            Decision(op="fill", target="member_input", input="unknown", reason="enter_parameter"),
            "undeclared_input",
        ),
    ],
)
async def test_model_cannot_claim_success_or_invent_actions(live, tmp_path, decision, code):
    async with live() as (ex, _):
        path = tmp_path / "absent.json"
        artifact, result = await discover(
            ex, ScriptedPlanner([decision]), TASK, {"member_id": "00123"}, path
        )
        assert artifact is None and not path.exists()
        assert result.failure.code == code
        assert result.failure.intervention_id


async def test_no_progress_is_bounded(live, tmp_path):
    decision = Decision(
        op="fill", target="member_input", input="member_id", reason="enter_parameter"
    )
    async with live() as (ex, _):
        artifact, result = await discover(
            ex,
            ScriptedPlanner([decision] * 4),
            TASK,
            {"member_id": "00123"},
            tmp_path / "absent.json",
        )
        assert artifact is None
        assert result.failure.code == "no_progress"


async def test_no_progress_recognizes_a_repeated_click_despite_its_learned_receipt(live, tmp_path):
    """A click's `after` is learned from the observed effect and overwrites the raw
    decision before it lands in history (discover()'s `decision.model_copy(update=
    {"after": ...})` right after learn_click()) -- so comparing raw decisions
    against history verbatim can never match for a click, and a model that keeps
    choosing the identical click silently burns the whole step budget instead of
    tripping the cheap no_progress guard.

    A real ModelPlanner always sends `after == target` as a placeholder
    (planner.parse_call), so the scripted decision below is exactly what a stuck
    model would send every time. "search" is monkeypatched to be genuinely
    repeatable: its effect (a "Search results" heading) is faked in and withdrawn
    between observations, exactly like a one-shot confirmation receipt, instead of
    the real page navigation -- so the SAME control is legitimately clickable
    again on the very next step.
    """
    async with live() as (ex, _):
        ex.policy.binding = ex.policy.binding.model_copy(update={"max_steps": 4})
        clicks = 0
        pending_remove = False
        original_perform = ex.surface.perform
        original_observe = ex.surface.observe

        async def perform(step, arguments):
            nonlocal clicks, pending_remove
            if step.target != "search":
                return await original_perform(step, arguments)
            clicks += 1
            frame = ex.surface.page.frame(name="workbench")
            await frame.evaluate(
                "() => {"
                " document.getElementById('probe-results')?.remove();"
                " const h = document.createElement('h2');"
                " h.id = 'probe-results';"
                " h.textContent = 'Search results';"
                " document.body.appendChild(h); }"
            )
            pending_remove = True
            return None

        async def observe():
            nonlocal pending_remove
            snapshot = await original_observe()
            if pending_remove:
                frame = ex.surface.page.frame(name="workbench")
                await frame.evaluate("() => document.getElementById('probe-results')?.remove()")
                pending_remove = False
            return snapshot

        ex.surface.perform = perform
        ex.surface.observe = observe

        decision = Decision(op="click", target="search", after="search", reason="follow_navigation")
        artifact, result = await discover(
            ex,
            ScriptedPlanner([decision] * 4),
            TASK,
            {"member_id": "00123"},
            tmp_path / "absent.json",
        )
        assert artifact is None
        # BUGGY code: no_progress never fires for a click, so all 4 identical
        # clicks are performed and the run dies with the far less diagnostic
        # step_budget_exhausted instead of no_progress.
        # FIXED code: the loop guard recognizes the 3rd identical click and stops
        # the run immediately with no_progress, after only 2 real clicks.
        assert result.failure.code == "no_progress"
        assert clicks == 2


async def test_discovery_shares_the_engine_state_precedence(live, tmp_path):
    """discover() observes states through the same Execution.settle() Replay
    uses -- a page showing both a business-outcome heading and a failure
    heading must report the failure through discovery too, confirming the two
    entry points genuinely share one precedence rule instead of each having
    its own copy that could drift.
    """
    async with live() as (ex, _):
        original_navigate = ex.surface.navigate

        async def navigate():
            await original_navigate()
            desk = ex.surface.page.frame(name="workbench")
            await desk.set_content("<h2>Member not found</h2><h2>Service unavailable</h2>")

        ex.surface.navigate = navigate
        artifact, result = await discover(
            ex, ScriptedPlanner([]), TASK, {"member_id": "00123"}, tmp_path / "absent.json"
        )
        assert artifact is None
        assert result.status == "failure"
        assert result.failure.code == "service_unavailable"


async def test_discovery_resumes_after_real_session_recovery(live, capability, tmp_path):
    async with live("expired") as (ex, _):
        original_page = ex.surface.page

        async def operator(owner, request, validate):
            lease = await owner.claim(request)
            frame = original_page.frame(name="workbench")
            await frame.get_by_role("button", name="Renew session", exact=True).click()
            await frame.get_by_role("heading", name="Account directory", exact=True).wait_for()
            await owner.resume(lease, validate)

        ex.handoff.operator = operator
        artifact, result = await discover(
            ex,
            ScriptedPlanner(decisions(capability)),
            TASK,
            {"member_id": "00123"},
            tmp_path / "recovered.json",
        )
        assert result.status == "success", result
        assert len(artifact.steps) == 6
        assert ex.handoff.ownership.manual_targets == {"renew_session"}
        assert original_page is ex.surface.page


async def test_unrecorded_manual_business_steps_do_not_become_capability(
    live, capability, tmp_path
):
    scripted = [Decision(op="stop", reason="cannot_proceed"), *list(decisions(capability))[2:]]
    async with live() as (ex, _):

        async def operator(owner, request, validate):
            lease = await owner.claim(request)
            frame = ex.surface.page.frame(name="workbench")
            await frame.get_by_label("Member identifier", exact=True).fill("00123")
            await frame.get_by_role("button", name="Find member", exact=True).click()
            await frame.get_by_role("heading", name="Search results", exact=True).wait_for()
            await owner.resume(lease, validate)

        ex.handoff.operator = operator
        path = tmp_path / "unreplayable.json"
        artifact, result = await discover(
            ex, ScriptedPlanner(scripted), TASK, {"member_id": "00123"}, path
        )
        assert artifact is None and not path.exists()
        assert result.failure.code == "manual_flow_requires_recording", result


async def test_step_budget_does_not_emit_partial_artifact(live, tmp_path):
    async with live() as (ex, _):
        ex.policy.binding = ex.policy.binding.model_copy(update={"max_steps": 1})
        planner = ScriptedPlanner(
            [
                Decision(
                    op="fill", target="member_input", input="member_id", reason="enter_parameter"
                )
            ]
        )
        artifact, result = await discover(
            ex, planner, TASK, {"member_id": "00123"}, tmp_path / "absent.json"
        )
        assert artifact is None and result.failure.code == "step_budget_exhausted"


@pytest.mark.parametrize("arguments", [{}, {"member_id": 123}, {"member_id": "SECRET"}])
async def test_discovery_invalid_input_never_navigates(live, tmp_path, arguments):
    async with live() as (ex, app):
        artifact, result = await discover(
            ex, ScriptedPlanner([]), TASK, arguments, tmp_path / "absent.json"
        )
        assert artifact is None and result.failure.code == "invalid_input"
        assert app.state.sessions == {}


@pytest.mark.parametrize(
    "case,code",
    [
        ("duplicate", "ambiguous_target"),
        ("checkpoint", "undeclared_checkpoint"),
        ("output", "undeclared_or_duplicate_output"),
        ("malformed_money", "output_type_mismatch"),
        ("business", "member_not_found"),
        ("timeout", "discovery_timeout"),
        ("exception", "discovery_error"),
    ],
)
async def test_discovery_failure_never_leaves_partial_artifact(
    live, capability, tmp_path, case, code
):
    import asyncio
    from unittest.mock import AsyncMock

    scripted = list(decisions(capability))
    if case == "duplicate":
        scripted = [scripted[1]]
    if case == "checkpoint":
        scripted[1] = scripted[1].model_copy(update={"after": "unknown"})
    if case == "output":
        scripted[-2] = scripted[-2].model_copy(update={"output": "unknown"})
    scenario = case if case in {"duplicate", "malformed_money"} else "normal"
    async with live(scenario) as (ex, _):
        planner = ScriptedPlanner(scripted)
        if case == "timeout":
            ex.policy.binding = ex.policy.binding.model_copy(update={"run_timeout": 0.03})

            async def delayed(_):
                await asyncio.sleep(1)

            planner.decide = delayed
        if case == "exception":
            planner.decide = AsyncMock(side_effect=RuntimeError("SECRET provider fault"))
        path = tmp_path / "absent.json"
        artifact, result = await discover(
            ex, planner, TASK, {"member_id": "00999" if case == "business" else "00123"}, path
        )
        assert artifact is None and not path.exists()
        if case == "business":
            assert result.status == "business_outcome" and result.code == code
            assert ex.handoff.ownership.owner == "automation"
        else:
            assert result.failure.code == code, result
        assert "SECRET" not in result.model_dump_json()


@pytest.mark.parametrize("human", [False, True])
async def test_model_fault_handoff_and_retry(live, capability, tmp_path, human):
    from computer_use_replay.policy import Stop

    class InterruptedPlanner(ScriptedPlanner):
        failed = False

        async def decide(self, context):
            if not self.failed:
                self.failed = True
                raise Stop("model_unavailable")
            return await super().decide(context)

    async with live() as (ex, _):
        if human:

            async def operator(owner, request, validate):
                lease = await owner.claim(request)
                await owner.resume(lease, validate)

            ex.handoff.operator = operator
        artifact, result = await discover(
            ex,
            InterruptedPlanner(decisions(capability)),
            TASK,
            {"member_id": "00123"},
            tmp_path / "artifact.json",
        )
        if human:
            assert artifact is not None and result.status == "success"
            events = (ex.evidence.directory / "events.jsonl").read_text()
            assert "control_resumed" in events
        else:
            assert artifact is None and result.failure.code == "model_unavailable"


async def test_verified_outputs_complete_without_an_extra_model_call(live, capability, tmp_path):
    from unittest.mock import AsyncMock

    scripted = list(decisions(capability))[:-1]
    planner = ScriptedPlanner(scripted)
    planner.decide = AsyncMock(side_effect=planner.decide)
    async with live() as (ex, _):
        artifact, result = await discover(
            ex, planner, TASK, {"member_id": "00123"}, tmp_path / "verified.json"
        )
        assert result.status == "success", result
        assert artifact is not None and len(artifact.steps) == 6
        assert planner.decide.await_count == 6


async def test_collected_output_does_not_bypass_wrong_member_checkpoint(live, capability, tmp_path):
    async with live("wrong_member") as (ex, _):
        artifact, result = await discover(
            ex,
            ScriptedPlanner(decisions(capability)),
            TASK,
            {"member_id": "00123"},
            tmp_path / "absent.json",
        )
        assert artifact is None
        assert result.failure.code == "checkpoint_failed"


async def test_identity_repaired_only_after_read_cannot_authorize_output(
    live, capability, tmp_path
):
    async with live("wrong_member") as (ex, _):

        class SettlingPlanner(ScriptedPlanner):
            async def decide(self, context):
                if context["collected_outputs"]:
                    await (
                        ex.surface.page.frame(name="workbench")
                        .get_by_role("row")
                        .filter(has_text="Member identifier")
                        .locator("td")
                        .evaluate("el=>el.textContent='00123'")
                    )
                return await super().decide(context)

        artifact, result = await discover(
            ex,
            SettlingPlanner(decisions(capability)),
            TASK,
            {"member_id": "00123"},
            tmp_path / "settled.json",
        )
        assert artifact is None
        assert result.failure.code == "checkpoint_failed"


async def test_discovery_records_observed_screen_instead_of_incorrect_prediction(
    live, capability, tmp_path
):
    scripted = list(decisions(capability))
    scripted[1] = scripted[1].model_copy(update={"after": "member_screen"})
    async with live() as (ex, _):
        artifact, result = await discover(
            ex, ScriptedPlanner(scripted), TASK, {"member_id": "00123"}, tmp_path / "learned.json"
        )
        assert result.status == "success", result
        assert artifact.steps[1].after.target == "results_screen"
        assert "checkpoint_observed" in (ex.evidence.directory / "events.jsonl").read_text()


@pytest.mark.parametrize(
    "fault,code",
    [
        ("ambiguous", "ambiguous_checkpoint"),
        ("unchanged", "checkpoint_failed"),
        ("uncertain", None),
        ("blocked", "action_policy"),
    ],
)
async def test_discovery_click_receipt_is_observed_and_never_retried(live, fault, code):
    from unittest.mock import AsyncMock

    from computer_use_replay.contracts import Click, Condition
    from computer_use_replay.discovery import learn_click
    from computer_use_replay.policy import Stop

    async with live() as (ex, _):
        await ex.surface.navigate()
        frame = ex.surface.page.frame(name="workbench")
        await frame.get_by_label("Member identifier").fill("00123")
        before = await ex.surface.observe()
        original = ex.surface.perform

        async def effect(step, arguments):
            if fault == "blocked":
                raise Stop("action_policy")
            if fault != "unchanged":
                await original(step, arguments)
            if fault == "ambiguous":
                await frame.get_by_role("heading", name="Search results").evaluate(
                    "el=>el.insertAdjacentHTML('afterend','<h2>Member summary</h2>')"
                )
            if fault == "uncertain":
                raise Stop("effect_uncertain")

        ex.surface.perform = AsyncMock(side_effect=effect)
        step = Click(target="search", after=Condition(target="member_screen"))
        if code:
            with pytest.raises(Stop, match=code):
                await learn_click(ex, step, {"member_id": "00123"}, 1, before)
        else:
            learned = await learn_click(ex, step, {"member_id": "00123"}, 1, before)
            assert learned.after.target == "results_screen"
        assert ex.surface.perform.await_count == 1


async def test_discovery_rejects_wrong_text_output_source(live, capability, tmp_path):
    scripted = list(decisions(capability))
    scripted[-2] = scripted[-2].model_copy(update={"target": "member_identity"})
    async with live() as (ex, _):
        artifact, result = await discover(
            ex, ScriptedPlanner(scripted), TASK, {"member_id": "00123"}, tmp_path / "absent.json"
        )
        assert artifact is None and result.failure.code == "output_source_mismatch"


async def test_identity_drift_during_output_read_prevents_completion(live, capability, tmp_path):
    from computer_use_replay.contracts import Read

    async with live() as (ex, _):
        original = ex.surface.perform

        async def drifting_read(step, arguments):
            value = await original(step, arguments)
            if isinstance(step, Read):
                await (
                    ex.surface.page.frame(name="workbench")
                    .get_by_role("row")
                    .filter(has_text="Member identifier")
                    .locator("td")
                    .evaluate("el=>el.textContent='00999'")
                )
            return value

        ex.surface.perform = drifting_read
        artifact, result = await discover(
            ex,
            ScriptedPlanner(decisions(capability)),
            TASK,
            {"member_id": "00123"},
            tmp_path / "drift.json",
        )
        assert artifact is None
        assert result.failure.code == "checkpoint_failed"


async def test_explicit_non_heading_marker_is_learned_and_replayed(live, capability, tmp_path):
    from computer_use_replay.contracts import Target
    from computer_use_replay.policy import Binding

    async with live() as (ex, _):
        raw = ex.policy.binding.model_dump(mode="json")
        raw["controls"]["results_screen"]["target"] = Target(
            frames=("workbench",), kind="css", name='h2:text-is("Search results")'
        ).model_dump(mode="json")
        raw["controls"]["results_screen"]["checkpoint"] = True
        ex.policy.binding = Binding.model_validate(raw)
        changed_binding = ex.policy.binding
        artifact, result = await discover(
            ex,
            ScriptedPlanner(decisions(capability)),
            TASK,
            {"member_id": "00123"},
            tmp_path / "marker.json",
        )
        assert result.status == "success", result
    async with live() as (ex, _):
        ex.policy.binding = changed_binding
        result = await Replay(ex).run(artifact, {"member_id": "00456"})
        assert result.status == "success", result
        assert result.outputs["available_balance"]["amount"] == "8902.10"


@pytest.mark.parametrize("drift", [False, True])
@pytest.mark.parametrize("prefilled", [False, True])
async def test_field_feedback_uses_current_values_not_only_history(
    live, capability, tmp_path, drift, prefilled
):
    async with live() as (ex, _):
        navigate = ex.surface.navigate

        async def initial_value():
            await navigate()
            if prefilled:
                await (
                    ex.surface.page.frame(name="workbench")
                    .get_by_label("Member identifier", exact=True)
                    .fill("00123")
                )

        ex.surface.navigate = initial_value
        perform = ex.surface.perform
        filled = False

        async def external_change(step, arguments):
            nonlocal filled
            result = await perform(step, arguments)
            if step.op == "fill" and not filled:
                filled = True
                if drift:
                    await (
                        ex.surface.page.frame(name="workbench")
                        .get_by_label("Member identifier", exact=True)
                        .fill("00456")
                    )
            return result

        ex.surface.perform = external_change
        sequence = list(decisions(capability))
        if drift:
            sequence.insert(1, sequence[0])

        class Planner(ScriptedPlanner):
            calls = 0

            async def decide(self, context):
                if self.calls == 0:
                    assert "member_input" not in context["verified_completed_fields"]
                if self.calls == 1:
                    assert ("member_input" in context["verified_completed_fields"]) is not drift
                    assert ("fill" in context["catalog"]["member_input"]["operations"]) is drift
                    assert context["catalog"]["member_input"]["allowed_inputs"] == (
                        ["member_id"] if drift else []
                    )
                assert "00123" not in json.dumps(context)
                assert "00456" not in json.dumps(context)
                self.calls += 1
                return await super().decide(context)

        artifact, result = await discover(
            ex, Planner(sequence), TASK, {"member_id": "00123"}, tmp_path / "feedback.json"
        )
        assert result.status == "success", result
        assert artifact is not None


async def test_stale_decision_is_reobserved_before_dispatch(live, capability, tmp_path):
    from computer_use_replay.contracts import Target
    from computer_use_replay.policy import Binding

    async with live() as (ex, _):
        raw = ex.policy.binding.model_dump(mode="json")
        raw["controls"]["refreshed_input"] = {
            **raw["controls"]["member_input"],
            "target": Target(
                kind="label", name="Refreshed identifier", frames=("workbench",)
            ).model_dump(mode="json"),
        }
        ex.policy.binding = Binding.model_validate(raw)
        choices = list(decisions(capability))
        choices.insert(1, choices[0].model_copy(update={"target": "refreshed_input"}))

        class Planner(ScriptedPlanner):
            changed = False

            async def decide(self, context):
                decision = await super().decide(context)
                if not self.changed:
                    self.changed = True
                    await (
                        ex.surface.page.frame(name="workbench")
                        .get_by_label("Member identifier", exact=True)
                        .evaluate("el=>el.labels[0].textContent='Refreshed identifier'")
                    )
                return decision

        artifact, result = await discover(
            ex, Planner(choices), TASK, {"member_id": "00123"}, tmp_path / "fresh.json"
        )
        assert result.status == "success", result
        assert artifact.steps[0].target == "refreshed_input"
        events = [
            json.loads(line)
            for line in (ex.evidence.directory / "events.jsonl").read_text().splitlines()
        ]
        assert any(e.get("code") == "surface_changed_before_action" for e in events)
        assert not any(
            e["event"] == "action_started" and e.get("target") == "member_input" for e in events
        )
