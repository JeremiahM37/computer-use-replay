"""Frame and dialog identity, row scopes, and permitted input bindings."""

import html
import json
from pathlib import Path

import pytest

from computer_use_replay.browser import BrowserSurface
from computer_use_replay.contracts import (
    Condition,
    Fill,
    GoalRequest,
    Input,
    Read,
    Target,
    TargetScope,
)
from computer_use_replay.control import Ownership
from computer_use_replay.discovery import discover
from computer_use_replay.evidence import Evidence
from computer_use_replay.planner import action_tools
from computer_use_replay.policy import Binding, Control, Policy, Stop
from tests.support.planner import ScriptedPlanner, decisions


@pytest.mark.parametrize("attribute", [None, "data-name"])
@pytest.mark.parametrize("value", ["CP-PUMP-100", 'x"], .other, [data-name="x', "日本語 \\.*"])
async def test_scoped_row_fill_read_and_ambiguity(tmp_path, attribute, value):
    scope = TargetScope(container=".row", anchor="a, span.identity", attribute=attribute)
    target = Target(kind="css", name="input", scope=scope)
    binding = Binding(
        product="grid",
        entry="/",
        routes={"/": ("GET",)},
        controls={
            "quantity": Control(
                target=target,
                match_input="item",
                description="Quantity of requested item",
                operations=("fill", "read"),
                allowed_inputs=("quantity",),
            )
        },
        states={},
        input_types={"item": Input(kind="text"), "quantity": Input(kind="text")},
        invariants=(Condition(target="quantity"),),
    )
    evidence = Evidence(tmp_path)
    async with BrowserSurface(
        Policy(binding, "http://localhost"), Ownership(evidence), evidence
    ) as surface:
        identity = html.escape(value, quote=True)
        row = (
            '<section class="row"><a data-name="'
            + identity
            + '">'
            + identity
            + '</a><input value="1"></section>'
        )
        other = '<section class="row"><a data-name="other">other</a><input value="99"></section>'
        await surface.page.set_content(other + row)
        assert await surface.count("quantity") == 0
        surface.bind_arguments({"item": value})
        assert await surface.count("quantity") == 1
        await surface.perform(Fill(target="quantity", input="quantity"), {"quantity": "4"})
        assert await surface.perform(Read(target="quantity", output="result"), {}) == "4"
        assert await surface.page.locator(".row").first.locator("input").input_value() == "99"
        # Reordering never changes which product is selected.
        await surface.page.locator(".row").last.evaluate("el => el.parentNode.prepend(el)")
        assert await surface.perform(Read(target="quantity", output="result"), {}) == "4"
        surface.bind_arguments({"item": "other"})
        assert await surface.perform(Read(target="quantity", output="result"), {}) == "99"
        surface.bind_arguments({"item": value})
        await surface.page.locator(".row").first.evaluate("el => el.after(el.cloneNode(true))")
        with pytest.raises(Stop, match="ambiguous_target"):
            await surface._unique("quantity")
        await surface.page.locator(".row").nth(1).locator("a").evaluate("el => el.hidden = true")
        assert await surface.count("quantity") == 1
        if attribute:
            surface.bind_arguments({"item": "\0"})
            assert await surface.count("quantity") == 0
        surface.bind_arguments({"item": "missing"})
        assert await surface.count("quantity") == 0
        with pytest.raises(Stop, match="target_missing"):
            await surface._unique("quantity")


async def test_scoped_identity_stays_in_named_frame(live):
    async with live() as (ex, _):
        await ex.surface.navigate()
        frame = ex.surface.page.frame(name="workbench")
        markup = '<div class="row"><span>12345</span><input value="17"></div>'
        await frame.set_content(markup)
        await ex.surface.page.locator("body").evaluate(
            "(el, markup) => el.insertAdjacentHTML('beforeend', markup)", markup
        )
        controls = dict(ex.policy.binding.controls)
        controls["balance"] = controls["balance"].model_copy(
            update={
                "target": Target(
                    frames=("workbench",),
                    kind="css",
                    name="input",
                    scope=TargetScope(container=".row", anchor="span"),
                ),
                "match_input": "member_id",
            }
        )
        ex.policy.binding = ex.policy.binding.model_copy(update={"controls": controls})
        ex.surface.bind_arguments({"member_id": "12345"})
        assert await ex.surface.perform(Read(target="balance", output="value"), {}) == "17"


@pytest.mark.parametrize("kind", ["aria", "native", "alert"])
async def test_aliases_do_not_hide_unknown_modals(tmp_path, kind):
    binding = Binding(
        product="modals",
        entry="/",
        routes={"/": ("GET",)},
        controls={
            "known": Control(
                target=Target(kind="css", name="#known"), description="Reviewed dialog"
            ),
            "alias": Control(
                target=Target(kind="css", name="[data-approved]"), description="Same dialog"
            ),
            "ambiguous": Control(
                target=Target(kind="css", name=".modal"), description="Ambiguous scope"
            ),
        },
        states={},
        input_types={},
        invariants=(Condition(target="known"),),
    )
    evidence = Evidence(tmp_path)
    async with BrowserSurface(
        Policy(binding, "http://localhost"), Ownership(evidence), evidence
    ) as surface:
        known = (
            '<dialog open id="known" data-approved class="modal">Known</dialog>'
            if kind == "native"
            else '<div role="'
            + ("alertdialog" if kind == "alert" else "dialog")
            + '" id="known" data-approved class="modal">Known</div>'
        )
        await surface.page.set_content(
            known
            + '<div role="dialog" class="modal">Unexpected</div><div role="dialog" hidden>Hidden</div>'
        )
        assert (await surface.observe()).unknown_dialogs == 1
        await surface.page.locator("#known").evaluate(
            "el=>el.insertAdjacentHTML('afterend','<iframe srcdoc=\"&lt;div role=dialog&gt;Foreign&lt;/div&gt;\"></iframe>')"
        )
        await surface.page.frame_locator("iframe").get_by_role("dialog").wait_for()
        assert (await surface.observe()).unknown_dialogs == 2


async def test_stale_parameter_scope_does_not_authorize_modal(tmp_path):
    binding = Binding(
        product="modals",
        entry="/",
        routes={"/": ("GET",)},
        controls={
            "known": Control(
                target=Target(kind="css", name='[role="dialog"]'),
                match_input="caption",
                description="Requested dialog",
            ),
        },
        states={},
        input_types={"caption": Input(kind="text")},
        invariants=(Condition(target="known"),),
    )
    evidence = Evidence(tmp_path)
    async with BrowserSurface(
        Policy(binding, "http://localhost"), Ownership(evidence), evidence
    ) as surface:
        await surface.page.set_content('<div role="dialog">Known</div>')
        surface.bind_arguments({"caption": "Known"})
        snapshot = await surface.observe()
        assert snapshot.unknown_dialogs == 0
        surface.bind_arguments({})
        assert await surface._unknown_dialogs(snapshot.controls) == 1


@pytest.mark.parametrize(
    "target,parameter", [("member_input", "nickname"), ("nickname_input", "member_id")]
)
async def test_browser_blocks_cross_field_parameters_before_fill(live, target, parameter):
    async with live() as (ex, _):
        await ex.surface.navigate()
        with pytest.raises(Stop, match="input_target_mismatch"):
            await ex.surface.perform(
                Fill(target=target, input=parameter),
                {"member_id": "00123", "nickname": "SECRET alias"},
            )
        assert not (ex.evidence.directory / "events.jsonl").exists()


def test_saved_artifact_cannot_swap_parameter(binding, subaccount_capability):
    steps = list(subaccount_capability.steps)
    steps[0] = Fill(target="member_input", input="nickname")
    with pytest.raises(Stop, match="input_target_mismatch"):
        Policy(binding, "http://localhost").check_artifact(
            subaccount_capability.model_copy(update={"steps": tuple(steps)})
        )


@pytest.mark.parametrize("fault", ["missing", "unknown", "nonfill"])
def test_binding_requires_explicit_valid_parameter_mapping(binding, fault):
    raw = binding.model_dump(mode="json")
    if fault == "missing":
        raw["controls"]["member_input"]["allowed_inputs"] = []
    elif fault == "unknown":
        raw["controls"]["member_input"]["allowed_inputs"] = ["undefined"]
    else:
        raw["controls"]["search"]["allowed_inputs"] = ["member_id"]
    with pytest.raises(ValueError):
        Binding.model_validate(raw)


async def test_discovery_logs_reference_and_rejects_wrong_binding(
    live, subaccount_capability, tmp_path
):
    request = GoalRequest.load(Path("requests/prepare_subaccount.json"))
    first = next(decisions(subaccount_capability)).model_copy(update={"input": "nickname"})
    async with live() as (ex, _):
        artifact, result = await discover(
            ex,
            ScriptedPlanner([first]),
            request,
            {"member_id": "00123", "nickname": "SECRET alias"},
            tmp_path / "never.json",
        )
        assert artifact is None and result.failure.code == "input_target_mismatch"
        raw = (ex.evidence.directory / "events.jsonl").read_text()
        events = [json.loads(line) for line in raw.splitlines()]
        decision = next(row for row in events if row["event"] == "decision")
        assert decision["target"] == "member_input" and decision["parameter"] == "nickname"
        assert not any(row["event"] == "action_started" for row in events)
        assert "SECRET alias" not in raw and "00123" not in raw


def test_offered_parameters_follow_visible_control_binding():
    context = {
        "inputs": {"member_id": {}, "nickname": {}},
        "outputs": {},
        "observation": {"controls": [{"target": "member_input", "count": 1}]},
        "catalog": {
            "member_input": {
                "operations": ["fill"],
                "risk": "reversible",
                "allowed_inputs": ["member_id"],
            }
        },
    }
    fill = next(
        t["function"] for t in action_tools(context) if t["function"]["name"] == "fill_control"
    )
    assert fill["parameters"]["properties"]["parameter"]["enum"] == ["member_id"]
    context["inputs"] = {"nickname": {}}
    assert not any(t["function"]["name"] == "fill_control" for t in action_tools(context))


@pytest.mark.parametrize("task", ["read_savings", "prepare_subaccount"])
@pytest.mark.parametrize(
    "member,code",
    [("00999", "member_not_found"), ("00888", "permission_denied"), ("00000", "validation_error")],
)
async def test_discovery_business_outcome_has_no_intervention(
    live, capability, subaccount_capability, tmp_path, task, member, code
):
    class CountingPlanner(ScriptedPlanner):
        async def decide(self, context):
            self.calls += 1
            return await super().decide(context)

    request = GoalRequest.load(Path(f"requests/{task}.json"))
    selected = capability if task == "read_savings" else subaccount_capability
    args = {"member_id": member}
    if task == "prepare_subaccount":
        args["nickname"] = "Private alias"
    path = tmp_path / "absent.json"
    async with live() as (ex, app):
        artifact, result = await discover(
            ex, CountingPlanner(decisions(selected)), request, args, path
        )
        assert artifact is None and not path.exists()
        assert result.status == "business_outcome" and result.code == code
        assert result.llm_calls == 2
        assert ex.handoff.ownership.owner == "automation" and app.state.finalizations == 0
        raw = (ex.evidence.directory / "events.jsonl").read_text()
        events = [json.loads(line) for line in raw.splitlines()]
        assert not any(
            row["event"] in {"failure", "intervention_requested", "artifact_saved"}
            for row in events
        )
        assert events[-1]["event"] == "business_outcome"
        started = next(
            row for row in events if row["event"] == "action_started" and row["op"] == "fill"
        )
        assert started["parameter"] == "member_id"
        assert member not in raw and "Private alias" not in raw
