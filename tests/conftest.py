from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from computer_use_replay.browser import BrowserSurface
from computer_use_replay.contracts import (
    Capability,
    Click,
    Condition,
    Fill,
    GoalRequest,
    Provenance,
    Read,
)
from computer_use_replay.control import Handoff, Ownership
from computer_use_replay.demo import serve_demo as workstation
from computer_use_replay.engine import Execution
from computer_use_replay.evidence import Evidence
from computer_use_replay.policy import Binding, Policy

TASK = GoalRequest.load(Path("requests/read_savings.json"))


@pytest.fixture
def binding():
    return Binding.load(Path("profiles/juniper.json"))


@pytest.fixture
def capability(binding):
    # Explicit hand-authored fixture, not submitted as genuine discovery evidence.
    return Capability(
        name="read_savings",
        product=binding.product,
        binding_sha256=binding.digest(),
        targets={k: v.target for k, v in binding.controls.items()},
        inputs=TASK.inputs,
        outputs=TASK.outputs,
        checkpoint=TASK.checkpoint,
        provenance=Provenance(mode="test_fixture", model="fixture", calls=0, run_id="test"),
        steps=(
            Fill(target="member_input", input="member_id"),
            Click(target="search", after=Condition(target="results_screen")),
            Click(target="open_member", after=Condition(target="member_screen")),
            Click(target="accounts", after=Condition(target="accounts_screen")),
            Click(target="savings", after=Condition(target="savings_screen")),
            Read(target="balance", output="available_balance"),
        ),
    )


@pytest.fixture
def live(tmp_path, binding):
    @asynccontextmanager
    async def create(scenario="normal", operator=None, present=False, pace=0.9):
        async with workstation(scenario) as (origin, app):
            evidence = Evidence(tmp_path)
            policy = Policy(binding, origin)
            ownership = Ownership(evidence)
            async with BrowserSurface(
                policy, ownership, evidence, present=present, pace=pace
            ) as surface:
                handoff = Handoff(ownership, operator)
                yield Execution(surface, policy, evidence, handoff), app

    return create


@pytest.fixture
def subaccount_capability(capability):
    task = GoalRequest.load(Path("requests/prepare_subaccount.json"))
    return capability.model_copy(
        update={
            "name": task.name,
            "inputs": task.inputs,
            "outputs": task.outputs,
            "checkpoint": task.checkpoint,
            "steps": capability.steps[:4]
            + (
                Click(target="subaccount", after=Condition(target="subaccount_screen")),
                Fill(target="nickname_input", input="nickname"),
                Click(target="review_subaccount", after=Condition(target="confirmation_screen")),
                Read(target="review_status", output="review_status"),
            ),
        }
    )
