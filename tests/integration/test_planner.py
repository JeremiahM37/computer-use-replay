"""Planner tool schemas built from offered controls, and provider-agnostic call parsing.

Transport, retry and malformed-response behavior is exercised generically across
providers (including ollama) in tests/integration/test_providers.py via ModelPlanner;
it is not duplicated here.
"""

from pathlib import Path

import pytest

from computer_use_replay.contracts import GoalRequest
from computer_use_replay.planner import action_tools, parse_call

TASK = GoalRequest.load(Path("requests/read_savings.json"))


@pytest.fixture
def context(binding):
    return {
        "inputs": binding.input_types,
        "outputs": {k: v.model_dump() for k, v in TASK.outputs.items()},
        "observation": {"controls": [{"target": "search", "count": 1}]},
        "catalog": {
            k: {"operations": v.operations, "risk": v.risk} for k, v in binding.controls.items()
        },
    }


def test_action_schema_excludes_unobserved_and_irreversible_controls(context):
    tools = action_tools(context)
    click = next(t for t in tools if t["function"]["name"] == "click_control")
    assert click["function"]["parameters"]["properties"]["target"]["enum"] == ["search"]
    assert {tool["function"]["name"] for tool in tools} == {"click_control", "request_help"}


@pytest.mark.parametrize(
    "args",
    [
        {"target": "finalize"},
        {"target": "search", "output": "member_screen"},
        {},
        "malformed",
    ],
)
def test_provider_arguments_are_checked_against_offered_tools(context, args):
    with pytest.raises(ValueError):
        parse_call(
            {"function": {"name": "click_control", "arguments": args}}, action_tools(context)
        )


async def test_live_context_preserves_alternatives_and_uses_current_values(binding):
    """A prior fill cannot prove a value still exists, or remove a valid alternative."""
    import json
    from types import SimpleNamespace

    from computer_use_replay.contracts import Input, Target
    from computer_use_replay.discovery import _build_planner_context
    from computer_use_replay.evidence import LiveCandidate, Snapshot
    from computer_use_replay.policy import Control

    request = TASK.model_copy(
        update={"inputs": {**TASK.inputs, "quantity": Input(kind="integer", minimum=1, maximum=10)}}
    )
    target = Target(kind="css", name='form input[name="lookup"]')
    candidate = LiveCandidate(
        candidate_id="live_field", scope="lookup", kind="field", role="field", locator=target
    )
    stale = candidate.model_copy(update={"candidate_id": "live_stale"})
    control = Control(
        target=target,
        operations=("fill",),
        risk="reversible",
        description="live scoped field",
        allowed_inputs=("member_id", "quantity", "not_in_request"),
    )
    matched = True
    checks = []

    async def condition(condition, arguments):
        checks.append(condition)
        return matched and condition.target == "live_field" and condition.input == "member_id"

    surface = SimpleNamespace(_live_controls={"live_field": control}, condition=condition)
    execution = SimpleNamespace(surface=surface)
    snapshot = Snapshot(controls=(), states=(), live_candidates=(candidate, stale))
    history = [{"op": "fill", "target": "live_field", "input": "member_id"}]
    context, _ = await _build_planner_context(
        execution, binding, request, {"member_id": "00123", "quantity": 2}, snapshot, history, {}
    )
    assert context["live_input_matches"] == {"live_field": ["member_id"]}
    assert context["live_verified_fields"] == []
    assert context["live_catalog"]["live_field"]["allowed_inputs"] == control.allowed_inputs
    assert "live_stale" not in context["live_catalog"]
    assert any(c.kind == "equals_integer_input" and c.input == "quantity" for c in checks)
    assert "00123" not in json.dumps(context)
    assert 'name="lookup"' not in json.dumps(context)

    matched = False
    cleared, _ = await _build_planner_context(
        execution, binding, request, {"member_id": "00123", "quantity": 2}, snapshot, history, {}
    )
    assert cleared["live_input_matches"] == {"live_field": []}
    fill = next(t for t in action_tools(cleared) if t["function"]["name"] == "fill_control")
    assert fill["function"]["parameters"]["properties"]["parameter"]["enum"] == [
        "member_id",
        "quantity",
    ]


def test_live_compiler_rejects_parameter_not_granted_to_selected_field():
    from types import SimpleNamespace

    from computer_use_replay.discovery import _compile_decision
    from computer_use_replay.planner import Decision
    from computer_use_replay.policy import Binding, Policy, Stop

    binding = Binding.load(Path("profiles/juniper_live.json"))
    request = GoalRequest.load(Path("requests/prepare_subaccount.json"))
    surface = SimpleNamespace(
        _live_controls={"live_lookup": SimpleNamespace(allowed_inputs=("member_id",))},
        _live_scopes={"live_lookup": ("lookup",)},
    )
    execution = SimpleNamespace(surface=surface, policy=Policy(binding, "http://localhost"))
    with pytest.raises(Stop) as caught:
        _compile_decision(
            execution,
            request,
            binding,
            Decision(op="fill", target="live_lookup", input="nickname", reason="enter_parameter"),
            {},
        )
    assert caught.value.code == "input_target_mismatch"
