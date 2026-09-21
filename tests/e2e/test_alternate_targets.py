"""Reviewed locator alternates: a rank-drift signal, never a fuzzy fallback ladder."""

import json
from pathlib import Path

import pytest

from computer_use_replay.browser import BrowserSurface
from computer_use_replay.contracts import Condition, Input, Target
from computer_use_replay.control import Handoff, Ownership
from computer_use_replay.demo import serve_demo
from computer_use_replay.engine import Execution, Replay
from computer_use_replay.evidence import Evidence
from computer_use_replay.policy import Binding, Control, Policy


@pytest.fixture
def tenant_b_binding(binding):
    return binding.overlay(Path("profiles/tenant_b.json"))


async def test_alternate_resolves_relabeled_control_and_logs_rank_one(
    tmp_path, capability, tenant_b_binding
):
    # tenant_b.json declares a reviewed alternate for "search": tenant B's own label
    # ("Search member") is primary, and the pre-rollout tenant-A label ("Find member")
    # is the fallback rung -- a realistic partial-rollout scenario, not a synthetic one.
    async with serve_demo("tenant_b") as (origin, app):
        evidence = Evidence(tmp_path)
        owner = Ownership(evidence)
        policy = Policy(tenant_b_binding, origin)
        async with BrowserSurface(policy, owner, evidence) as surface:
            navigate = surface.navigate

            async def not_yet_relabeled():
                # Simulate a page that hasn't picked up tenant B's relabel yet: the
                # primary reviewed target has zero visible matches on this page.
                await navigate()
                await (
                    surface.page.frame(name="operations")
                    .get_by_role("button", name="Search member", exact=True)
                    .evaluate("el => el.textContent = 'Find member'")
                )

            surface.navigate = not_yet_relabeled
            execution = Execution(surface, policy, evidence, Handoff(owner))
            result = await Replay(execution).run(capability, {"member_id": "00456"})
            assert result.status == "success", result
            assert result.outputs == {"available_balance": {"amount": "8902.10", "currency": "USD"}}
            assert result.llm_calls == 0 and app.state.finalizations == 0
    rows = [
        json.loads(line) for line in (evidence.directory / "events.jsonl").read_text().splitlines()
    ]
    resolved = [row for row in rows if row["event"] == "alternate_resolved"]
    # Exactly one: perform() logs it once per acted-upon step, not once per poll.
    assert len(resolved) == 1, resolved
    assert resolved[0]["target"] == "search" and resolved[0]["rank"] == 1


async def test_ambiguous_alternate_stops_without_trying_further(
    tmp_path, capability, tenant_b_binding
):
    async with serve_demo("tenant_b") as (origin, app):
        evidence = Evidence(tmp_path)
        owner = Ownership(evidence)
        policy = Policy(tenant_b_binding, origin)
        async with BrowserSurface(policy, owner, evidence) as surface:
            navigate = surface.navigate

            async def not_relabeled_and_duplicated():
                await navigate()
                frame = surface.page.frame(name="operations")
                await frame.get_by_role("button", name="Search member", exact=True).evaluate(
                    "el => el.textContent = 'Find member'"
                )
                # A second control offering the SAME alternate label makes rank 1
                # itself ambiguous. The ladder must stop there, not keep guessing.
                await frame.locator("body").evaluate(
                    "el => el.insertAdjacentHTML('beforeend', '<button>Find member</button>')"
                )

            surface.navigate = not_relabeled_and_duplicated
            execution = Execution(surface, policy, evidence, Handoff(owner))
            result = await Replay(execution).run(capability, {"member_id": "00456"})
    assert result.status == "failure", result
    assert result.failure.code == "ambiguous_target"
    assert result.failure.target == "search"
    assert app.state.finalizations == 0


async def test_primary_present_alternates_never_consulted(live):
    async with live() as (ex, _):
        await ex.surface.navigate()
        controls = dict(ex.policy.binding.controls)
        never_queried = Target(kind="css", name="#never-queried", frames=("workbench",))
        controls["search"] = controls["search"].model_copy(update={"alternates": (never_queried,)})
        ex.policy.binding = ex.policy.binding.model_copy(update={"controls": controls})
        queried = []
        original = ex.surface._locator

        def spy(target, match_input=None):
            queried.append(target)
            return original(target, match_input)

        ex.surface._locator = spy
        # "search" ("Find member") is genuinely visible on the page just navigated to.
        assert await ex.surface.count("search") == 1
        loc = await ex.surface._unique("search", log_alternate=True)
        assert await loc.count() == 1
    assert never_queried not in queried
    assert controls["search"].target in queried
    # count()/_unique() never emit on their own; nothing observed a resolvable step.
    assert not (ex.evidence.directory / "events.jsonl").exists()


async def test_alternate_targeting_cannot_rescue_a_wrong_member_checkpoint(tmp_path):
    # An alternate only changes WHICH element is targeted, never whether its
    # displayed value satisfies a checkpoint: locating a control through a
    # reviewed fallback rung must not "rescue" a semantically wrong result.
    binding = Binding(
        product="identity_check",
        entry="/",
        routes={"/": ("GET",)},
        controls={
            "identity": Control(
                target=Target(kind="css", name="#missing"),
                alternates=(Target(kind="css", name="#actual"),),
                operations=("read",),
                description="Member identifier display",
            ),
        },
        states={},
        input_types={"member_id": Input(kind="identifier", pattern=r"^\d{5}$")},
        invariants=(Condition(target="identity", kind="equals_input", input="member_id"),),
    )
    evidence = Evidence(tmp_path)
    async with BrowserSurface(
        Policy(binding, "http://localhost"), Ownership(evidence), evidence
    ) as surface:
        await surface.page.set_content('<div id="actual">00777</div>')
        condition = Condition(target="identity", kind="equals_input", input="member_id")
        # The alternate resolves the control uniquely -- one visible match, not zero.
        assert await surface.count("identity") == 1
        # But its displayed value is the wrong member: the checkpoint still fails.
        assert not await surface.condition(condition, {"member_id": "00123"})


def test_target_contract_has_no_alternates_field():
    # Alternates are a Control/Presentation (policy) concept, never Target (contracts):
    # a saved Capability's `targets` dict -- built purely from Target objects -- can
    # never carry a reviewed rung, structurally, regardless of what a profile declares.
    assert "alternates" not in Target.model_fields


def test_committed_artifact_bytes_have_no_alternates_key():
    # profiles/tenant_b.json's own reviewed alternate for "search" must not leak into
    # the tracked, byte-pinned discovery recording -- discovery only ever records the
    # primary target as a review hint.
    raw = Path("capabilities/read_savings.json").read_text()
    assert '"alternates"' not in raw
