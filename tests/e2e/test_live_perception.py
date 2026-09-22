"""Adversarial checks for the opt-in live-page perception path.

These tests use the real Chromium surface.  The planner is deliberately scripted
where the assertion is about grounding or policy; that proves the runtime
contract without claiming a genuine model result.
"""

import json
from pathlib import Path

import pytest

from computer_use_replay.browser_perception import LivePerception
from computer_use_replay.contracts import (
    Capability,
    Click,
    Condition,
    Fill,
    GoalRequest,
    Grounding,
    Read,
    Target,
)
from computer_use_replay.discovery import discover
from computer_use_replay.engine import Replay
from computer_use_replay.planner import Decision
from computer_use_replay.policy import Binding, Stop
from tests.support.planner import ScriptedPlanner

LIVE = Path("profiles/juniper_live.json")


def live_binding():
    return Binding.load(LIVE)


async def test_live_observation_exports_safe_candidates_only(live):
    async with live() as (ex, _):
        ex.policy.binding = live_binding()
        await ex.surface.navigate()
        await ex.surface.page.frame(name="workbench").evaluate(
            "() => document.querySelector('button').insertAdjacentHTML('beforeend', '<span data-private>PRIVATE-CANARY</span>')"
        )
        snapshot = await ex.surface.observe()
        candidates = snapshot.live_candidates
        assert candidates
        assert {candidate.scope for candidate in candidates} == {"lookup"}
        labels = {candidate.label for candidate in candidates}
        assert "Find member" in labels
        find = next(candidate for candidate in candidates if candidate.label == "Find member")
        assert find.ready is False
        # Private values and DOM implementation details never enter the
        # serialized observation, even though the browser has a live input.
        encoded = json.dumps(snapshot.model_dump(mode="json"))
        for secret in (
            "00123",
            "1204.57",
            "member_key",
            "PRIVATE-CANARY",
            "data-sensitive",
            "data-private",
        ):
            assert secret not in encoded
        assert all(candidate.candidate_id.startswith("live_") for candidate in candidates)


async def test_live_observation_omits_unknown_fields_and_non_native_roles(live):
    async with live() as (ex, _):
        ex.policy.binding = live_binding()
        await ex.surface.navigate()
        await ex.surface.page.frame(name="workbench").evaluate(
            "() => { const form=document.querySelector('form'); form.insertAdjacentHTML('beforeend', '<input name=admin_action value=delete><input role=checkbox aria-label=private><a href=/desk/delete>Delete</a>'); }"
        )
        snapshot = await ex.surface.observe()
        assert all(candidate.role != "checkbox" for candidate in snapshot.live_candidates)
        assert all(candidate.label != "Delete" for candidate in snapshot.live_candidates)
        assert all(
            "admin_action" not in (candidate.label or "") for candidate in snapshot.live_candidates
        )


async def test_live_field_reappears_after_a_successful_fill_is_cleared(live):
    async with live() as (ex, _):
        ex.policy.binding = live_binding()
        await ex.surface.navigate()
        ex.surface.bind_arguments({"member_id": "00123"})
        initial = await ex.surface.observe()
        field = next(
            candidate for candidate in initial.live_candidates if candidate.role == "field"
        )
        await ex.surface.perform(
            Fill(target=field.candidate_id, input="member_id"), {"member_id": "00123"}
        )
        await ex.surface.page.frame(name="workbench").locator("input").fill("")
        refreshed = await ex.surface.observe()
        assert any(candidate.role == "field" for candidate in refreshed.live_candidates)


async def test_live_scope_without_click_grant_omits_buttons_and_supports_no_static_text(live):
    async with live() as (ex, _):
        binding = live_binding().model_copy(
            update={
                "live_scopes": tuple(
                    scope.model_copy(update={"operations": ("fill",), "static_text": False})
                    for scope in live_binding().live_scopes
                )
            }
        )
        ex.policy.binding = binding
        await ex.surface.navigate()
        snapshot = await ex.surface.observe()
        assert snapshot.live_candidates
        assert all(candidate.role == "field" for candidate in snapshot.live_candidates)
        assert all(candidate.label is None for candidate in snapshot.live_candidates)


async def test_live_action_revalidates_scope_after_observation(live):
    async with live() as (ex, _):
        ex.policy.binding = live_binding()
        await ex.surface.navigate()
        snapshot = await ex.surface.observe()
        field = next(
            candidate for candidate in snapshot.live_candidates if candidate.role == "field"
        )
        await (
            ex.surface.page.frame(name="workbench")
            .locator("form")
            .evaluate("form => form.setAttribute('action', '/unreviewed')")
        )
        with pytest.raises(Stop, match="action_policy|ambiguous_target|live_candidate_stale"):
            await ex.surface.perform(
                Fill(target=field.candidate_id, input="member_id"), {"member_id": "00123"}
            )


async def test_live_action_cannot_dispatch_replacement_after_final_validation(live, monkeypatch):
    async with live() as (ex, _):
        ex.policy.binding = live_binding()
        await ex.surface.navigate()
        snapshot = await ex.surface.observe()
        field = next(
            candidate for candidate in snapshot.live_candidates if candidate.role == "field"
        )
        original = LivePerception.validate_action
        calls = 0

        async def validate_then_replace(perception, key, op):
            nonlocal calls
            result = await original(perception, key, op)
            calls += 1
            if calls == 2:
                await (
                    ex.surface.page.frame(name="workbench")
                    .locator("input")
                    .evaluate("el => el.replaceWith(el.cloneNode(true))")
                )
            return result

        monkeypatch.setattr(LivePerception, "validate_action", validate_then_replace)
        with pytest.raises(
            Exception, match="not attached|live_candidate_stale|fill_mismatch|effect_uncertain"
        ):
            await ex.surface.perform(
                Fill(target=field.candidate_id, input="member_id"), {"member_id": "00123"}
            )
        assert await ex.surface.page.frame(name="workbench").locator("input").input_value() == ""
        assert ex.surface._live_pinned_handles == {}


@pytest.mark.parametrize(
    ("markup", "name", "value", "expected", "assertion"),
    [
        (
            "<label for=agree>Agree</label><input id=agree name=agree type=checkbox>",
            "agree",
            True,
            True,
            "checked",
        ),
        (
            "<label for=choice>Choice</label><select id=choice name=choice><option value=00123>One</option><option value=00456>Two</option></select>",
            "choice",
            "Two",
            "00456",
            "value",
        ),
    ],
)
async def test_live_native_checkbox_and_select_fill(live, markup, name, value, expected, assertion):
    async with live() as (ex, _):
        base = live_binding()
        ex.policy.binding = base.model_copy(
            update={
                "live_scopes": tuple(
                    scope.model_copy(update={"fields": {**scope.fields, name: ("member_id",)}})
                    for scope in base.live_scopes
                )
            }
        )
        await ex.surface.navigate()
        await (
            ex.surface.page.frame(name="workbench")
            .locator("form")
            .evaluate("(form, markup) => form.insertAdjacentHTML('beforeend', markup)", markup)
        )
        snapshot = await ex.surface.observe()
        field = next(
            candidate
            for candidate in snapshot.live_candidates
            if candidate.role == "field" and candidate.label in {"Agree", "Choice"}
        )
        await ex.surface.perform(
            Fill(target=field.candidate_id, input="member_id"), {"member_id": value}
        )
        locator = ex.surface.page.frame(name="workbench").locator(f"[name={name}]")
        if assertion == "checked":
            assert await locator.is_checked()
        else:
            assert await locator.input_value() == expected


async def test_live_scope_cannot_override_authored_human_only_control(live):
    async with live() as (ex, _):
        ex.policy.binding = live_binding()
        await ex.surface.navigate()
        await ex.surface.page.frame(name="workbench").evaluate(
            "() => document.querySelector('button').insertAdjacentHTML('afterend', '<button>Finalize account closure</button>')"
        )
        snapshot = await ex.surface.observe()
        candidate = next(
            item for item in snapshot.live_candidates if item.label == "Finalize account closure"
        )
        with pytest.raises(Stop, match="human_required"):
            await ex.surface.perform(
                Click(
                    target=candidate.candidate_id,
                    after=Condition(target=candidate.candidate_id),
                ),
                {},
            )


async def test_live_candidate_id_is_stale_after_a_new_observation(live):
    async with live() as (ex, _):
        ex.policy.binding = live_binding()
        await ex.surface.navigate()
        first = await ex.surface.observe()
        first_id = first.live_candidates[0].candidate_id
        await ex.surface.page.frame(name="workbench").evaluate(
            "() => document.querySelector('form').insertAdjacentHTML('beforeend', '<button>Another</button>')"
        )
        second = await ex.surface.observe()
        assert first_id not in {candidate.candidate_id for candidate in second.live_candidates}
        assert len(ex.surface._live_history) == 0


async def test_live_scope_does_not_export_form_with_unreviewed_action(live):
    async with live() as (ex, _):
        ex.policy.binding = live_binding()
        await ex.surface.navigate()
        frame = ex.surface.page.frame(name="workbench")
        await frame.locator("form").evaluate("form => form.setAttribute('action', '/unreviewed')")
        snapshot = await ex.surface.observe()
        assert snapshot.live_candidates == ()


async def test_live_scope_rejects_formmethod_and_formaction_overrides(live):
    async with live() as (ex, _):
        ex.policy.binding = live_binding()
        await ex.surface.navigate()
        ex.surface.bind_arguments({"member_id": "00123"})
        await ex.surface.page.frame(name="workbench").evaluate(
            "() => { const b=document.querySelector('button'); b.setAttribute('formaction','/unreviewed'); b.setAttribute('formmethod','GET'); }"
        )
        snapshot = await ex.surface.observe()
        assert snapshot.live_candidates == ()


async def test_live_candidate_cap_fails_closed(live):
    async with live() as (ex, _):
        binding = live_binding().model_copy(update={"live_candidate_limit": 1})
        ex.policy.binding = binding
        await ex.surface.navigate()
        with pytest.raises(Stop, match="live_candidate_limit"):
            await ex.surface.observe()


async def test_replay_grounding_with_no_current_match_is_not_aliased(live):
    async with live() as (ex, _):
        ex.policy.binding = live_binding()
        ex.surface.install_groundings(
            {
                "saved": Grounding(
                    scope="lookup",
                    operation="fill",
                    target=Target(kind="css", name="form [name=missing]"),
                    role="field",
                    robustness="scope_css",
                )
            }
        )
        await ex.surface.navigate()
        await ex.surface.observe()
        assert "saved" not in ex.surface._live_controls


async def test_pinned_candidate_is_bounded_and_scope_revocation_rechecked(live):
    async with live() as (ex, _):
        ex.policy.binding = live_binding()
        await ex.surface.navigate()
        snapshot = await ex.surface.observe()
        field = next(
            candidate for candidate in snapshot.live_candidates if candidate.role == "field"
        )
        await ex.surface.pin_live_candidate(field.candidate_id)
        ex.policy.binding = ex.policy.binding.model_copy(update={"live_scopes": ()})
        with pytest.raises(Stop, match="action_policy"):
            await ex.surface.perform(
                Fill(target=field.candidate_id, input="member_id"), {"member_id": "00123"}
            )
        assert ex.surface._live_pinned_handles == {}


async def test_pinned_candidate_rejects_same_label_element_replacement(live):
    """A reviewed label is not authority to act on a replacement element."""
    async with live() as (ex, _):
        ex.policy.binding = live_binding()
        await ex.surface.navigate()
        snapshot = await ex.surface.observe()
        field = next(
            candidate for candidate in snapshot.live_candidates if candidate.role == "field"
        )
        await ex.surface.pin_live_candidate(field.candidate_id)
        await (
            ex.surface.page.frame(name="workbench")
            .locator("input")
            .evaluate("el => el.replaceWith(el.cloneNode(true))")
        )
        with pytest.raises(Stop, match="live_candidate_stale"):
            await ex.surface.perform(
                Fill(target=field.candidate_id, input="member_id"), {"member_id": "00123"}
            )
        assert ex.surface._live_pinned_handles == {}


async def test_root_excluded_control_and_unsupported_role_are_not_exported(live):
    async with live() as (ex, _):
        binding = live_binding()
        binding = binding.model_copy(
            update={
                "live_scopes": tuple(
                    scope.model_copy(update={"excluded": ("[data-private]",)})
                    for scope in binding.live_scopes
                )
            }
        )
        ex.policy.binding = binding
        await ex.surface.navigate()
        await ex.surface.page.frame(name="workbench").evaluate(
            "() => { const b=document.querySelector('button'); b.setAttribute('data-private','1'); b.insertAdjacentHTML('afterend','<div role=checkbox>Bad</div>'); }"
        )
        snapshot = await ex.surface.observe()
        assert all(item.label != "Find member" for item in snapshot.live_candidates)
        assert all(item.role != "checkbox" for item in snapshot.live_candidates)


async def test_excluded_associated_label_cannot_leak_static_text(live):
    async with live() as (ex, _):
        binding = live_binding().model_copy(
            update={
                "live_scopes": tuple(
                    scope.model_copy(update={"excluded": ("[data-private]",)})
                    for scope in live_binding().live_scopes
                )
            }
        )
        ex.policy.binding = binding
        await ex.surface.navigate()
        await ex.surface.page.frame(name="workbench").evaluate(
            "() => { const label=document.querySelector('label'); label.setAttribute('data-private','1'); label.textContent='PRIVATE-LABEL'; }"
        )
        snapshot = await ex.surface.observe()
        encoded = json.dumps(snapshot.model_dump(mode="json"))
        assert "PRIVATE-LABEL" not in encoded


async def test_live_labels_withheld_when_they_echo_sensitive_input(live):
    async with live() as (ex, _):
        ex.policy.binding = live_binding()
        await ex.surface.navigate()
        ex.surface.bind_arguments({"member_id": "00123"})
        await ex.surface.page.frame(name="workbench").evaluate(
            "() => document.querySelector('label').textContent='Member identifier: 00123'"
        )
        snapshot = await ex.surface.observe()
        encoded = json.dumps(snapshot.model_dump(mode="json"))
        assert "00123" not in encoded


async def test_live_field_name_is_structural_and_cannot_inject_a_selector(live):
    async with live() as (ex, _):
        ex.policy.binding = live_binding()
        await ex.surface.navigate()
        await ex.surface.page.frame(name="workbench").evaluate(
            "() => document.querySelector('input').setAttribute('name','member_key\"] input, body { display:none } /*')"
        )
        snapshot = await ex.surface.observe()
        assert all(candidate.role != "field" for candidate in snapshot.live_candidates)


async def test_duplicate_named_live_frames_fail_closed(live):
    async with live() as (ex, _):
        ex.policy.binding = live_binding()
        await ex.surface.navigate()
        await ex.surface.page.evaluate(
            "() => document.body.insertAdjacentHTML('beforeend', '<iframe name=workbench src=\\\"/desk/search\\\"></iframe>')"
        )
        with pytest.raises(Stop, match="ambiguous_frame"):
            await ex.surface.observe()


async def test_duplicate_live_candidates_fail_closed_at_action_time(live, tmp_path):
    class PickFirst:
        mode = "test_fixture"
        model = "scripted-perception"
        calls = 0

        async def decide(self, context):
            self.calls += 1
            candidate = next(
                item
                for item in context["observation"]["live_candidates"]
                if item.get("label") == "Find member"
            )
            return Decision(
                op="click",
                target=candidate["candidate_id"],
                after=candidate["candidate_id"],
                reason="follow_navigation",
            )

    async with live() as (ex, _):
        ex.policy.binding = live_binding()
        original_navigate = ex.surface.navigate

        async def navigate_with_duplicate():
            await original_navigate()
            await ex.surface.page.frame(name="workbench").evaluate(
                "() => { const input=document.querySelector('input'); input.value='00123'; input.dispatchEvent(new Event('input',{bubbles:true})); input.insertAdjacentHTML('afterend', '<button>Find member</button>'); }"
            )

        ex.surface.navigate = navigate_with_duplicate
        artifact, result = await discover(
            ex,
            PickFirst(),
            GoalRequest.load(Path("requests/read_savings.json")),
            {"member_id": "00123"},
            tmp_path / "duplicate.json",
        )
        assert artifact is None
        assert result.failure.code in {"ambiguous_target", "live_candidate_stale"}


async def test_live_scope_does_not_authorize_unseen_or_forged_candidate(live, tmp_path):
    async with live() as (ex, _):
        ex.policy.binding = live_binding()
        decision = Decision(
            op="fill", target="invented_candidate", input="member_id", reason="enter_parameter"
        )
        artifact, result = await discover(
            ex,
            ScriptedPlanner([decision]),
            GoalRequest.load(Path("requests/read_savings.json")),
            {"member_id": "00123"},
            tmp_path / "forged.json",
        )
        assert artifact is None
        assert result.failure.code == "ungrounded_target"
        assert not (tmp_path / "forged.json").exists()


async def test_live_replay_rechecks_scope_grant_before_browser_action(live, capability):
    binding = live_binding()
    dynamic = "live_member"
    dynamic_clicks = ("live_search", "live_open", "live_accounts", "live_savings")
    names = (dynamic, *dynamic_clicks)
    steps = (
        Fill(target=dynamic, input="member_id"),
        *(
            Click(target=key, after=Condition(target=after))
            for key, after in zip(
                dynamic_clicks,
                ("results_screen", "member_screen", "accounts_screen", "savings_screen"),
                strict=True,
            )
        ),
        Read(target="balance", output="available_balance"),
    )
    grounded = {
        key: Grounding(
            scope="removed_scope" if key == dynamic else "lookup",
            operation="fill" if key == dynamic else "click",
            target=Target(kind="css", name="form [name=member_key]"),
            role="field" if key == dynamic else "button",
            robustness="scope_css",
        )
        for key in names
    }
    artifact = capability.model_copy(
        update={
            "binding_sha256": binding.digest(),
            "targets": {
                **{
                    key: capability.targets[key]
                    for key in (
                        "balance",
                        "results_screen",
                        "member_screen",
                        "accounts_screen",
                        "savings_screen",
                    )
                },
                **{key: Target(kind="css", name="form [name=member_key]") for key in names},
            },
            "steps": steps,
            "grounded": grounded,
        }
    )
    async with live() as (ex, _):
        ex.policy.binding = binding
        with pytest.raises(Stop, match="action_policy"):
            ex.policy.binding = binding
            ex.policy.check_artifact(artifact)


async def test_live_discovery_compiles_groundings_and_replays_without_model(live, tmp_path):
    """A scripted planner stands in for the model; Chromium still performs every action."""

    class PerceptionPlanner:
        mode = "test_fixture"
        model = "scripted-perception"
        calls = 0

        async def decide(self, context):
            self.calls += 1
            candidates = context["observation"].get("live_candidates", [])
            if not context["history"]:
                field = next(item for item in candidates if item["role"] == "field")
                return Decision(
                    op="fill",
                    target=field["candidate_id"],
                    input="member_id",
                    reason="enter_parameter",
                )
            for candidate in candidates:
                if candidate.get("label") in {
                    "Find member",
                    "Open member",
                    "View accounts",
                    "Open savings ledger",
                }:
                    return Decision(
                        op="click",
                        target=candidate["candidate_id"],
                        after=candidate["candidate_id"],
                        reason="follow_navigation",
                    )
            if context["checkpoint_ready"] and not context["collected_outputs"]:
                return Decision(
                    op="read", target="balance", output="available_balance", reason="extract_output"
                )
            raise AssertionError(context)

    request = GoalRequest.load(Path("requests/read_savings.json"))
    binding = live_binding()
    path = tmp_path / "live-capability.json"
    planner = PerceptionPlanner()
    async with live() as (ex, app):
        ex.policy.binding = binding
        artifact, result = await discover(ex, planner, request, {"member_id": "00123"}, path)
        assert result.status == "success", result
        assert artifact.grounded
        assert all(key.startswith("live_") for key in artifact.grounded)
        assert app.state.finalizations == 0

    async with live() as (ex, app):
        ex.policy.binding = binding
        result = await Replay(ex).run(
            Capability.model_validate_json(path.read_text()), {"member_id": "00456"}
        )
        assert result.status == "success", result
        assert result.llm_calls == 0
        assert result.outputs["available_balance"]["amount"] == "8902.10"
        assert app.state.finalizations == 0
