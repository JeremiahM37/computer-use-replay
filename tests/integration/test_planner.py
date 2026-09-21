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
