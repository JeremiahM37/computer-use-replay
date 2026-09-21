"""Scripted model decisions with real browser, policy, and execution layers."""

from pathlib import Path

from computer_use_replay.contracts import GoalRequest
from computer_use_replay.planner import Decision

TASK = GoalRequest.load(Path("requests/read_savings.json"))


class ScriptedPlanner:
    """Test collaborator only: UI/server/serialization/interpreter are real."""

    mode = "test_fixture"
    model = "test_script"
    calls = 0

    def __init__(self, decisions):
        self.decisions = iter(decisions)

    async def decide(self, context):
        self.last_context = context
        assert set(context["catalog"]) == {
            node["target"] for node in context["observation"]["controls"]
        }
        return next(self.decisions)


def decisions(capability):
    for step in capability.steps:
        raw = step.model_dump(exclude_none=True)
        if step.op == "click":
            raw["after"] = step.after.target
        raw["reason"] = {
            "fill": "enter_parameter",
            "click": "follow_navigation",
            "read": "extract_output",
        }[step.op]
        yield Decision(**raw)
    yield Decision(op="done", reason="goal_complete")
