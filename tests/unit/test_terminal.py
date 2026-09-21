"""The second, deliberately basic surface (REPORT.md §4): same committed
web-learned capability, same engine and policy, a text-mode screen buffer
instead of a browser. No Playwright, no HTTP -- everything here is in-process
and fast.
"""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from computer_use_replay.contracts import (
    Capability,
    Click,
    Condition,
    Fill,
    GoalRequest,
    Read,
    Target,
)
from computer_use_replay.control import Handoff, Ownership
from computer_use_replay.discovery import discover
from computer_use_replay.engine import Execution, Replay
from computer_use_replay.evidence import Evidence
from computer_use_replay.policy import Binding, Control, Policy, Stop
from computer_use_replay.terminal import Entry, ScreenSurface, TextWorkstation
from tests.support.planner import ScriptedPlanner, decisions

TASK = GoalRequest.load(Path("requests/read_savings.json"))
BALANCE_A = {"available_balance": {"amount": "1204.57", "currency": "USD"}}
BALANCE_B = {"available_balance": {"amount": "8902.10", "currency": "USD"}}


def terminal_binding():
    return Binding.load(Path("profiles/juniper.json")).overlay(Path("profiles/terminal.json"))


def savings_capability():
    return Capability.model_validate_json(Path("capabilities/read_savings.json").read_text())


@pytest.fixture
def live(tmp_path):
    def create(scenario="normal"):
        binding = terminal_binding()
        evidence = Evidence(tmp_path)
        policy = Policy(binding, "https://juniper-terminal.invalid")
        owner = Ownership(evidence)
        surface = ScreenSurface(policy, owner, evidence, TextWorkstation(scenario))
        return Execution(surface, policy, evidence, Handoff(owner)), evidence

    return create


async def test_committed_capability_replays_unchanged_on_the_terminal_surface(live):
    path = Path("capabilities/read_savings.json")
    before = path.read_bytes()  # noqa: ASYNC240 — tiny one-shot repo-relative read, like elsewhere
    savings = Capability.model_validate_json(before)
    ex, evidence = live()
    result = await Replay(ex).run(savings, {"member_id": "00123"})
    assert result.status == "success", result
    assert result.outputs == BALANCE_A
    assert result.llm_calls == 0
    after = path.read_bytes()  # noqa: ASYNC240
    assert after == before
    assert hashlib.sha256(after).hexdigest() == hashlib.sha256(before).hexdigest()
    rows = [json.loads(x) for x in (evidence.directory / "events.jsonl").read_text().splitlines()]
    drift = [row for row in rows if row["event"] == "presentation_drift"]
    # The entire target *kind* changed (role/label/row_value -> screen), so every
    # recorded hint the capability uses differs -- same story as tenant_b, stronger.
    assert len(drift) == 1
    assert sorted(drift[0]["targets"]) == sorted(savings.targets)


async def test_changed_member_still_replays(live):
    ex, _ = live()
    result = await Replay(ex).run(savings_capability(), {"member_id": "00456"})
    assert result.status == "success", result
    assert result.outputs == BALANCE_B


@pytest.mark.parametrize(
    "member,code",
    [("99999", "member_not_found"), ("00888", "permission_denied"), ("00000", "validation_error")],
)
async def test_business_outcomes(live, member, code):
    ex, _ = live()
    result = await Replay(ex).run(savings_capability(), {"member_id": member})
    assert result.status == "business_outcome", result
    assert result.code == code
    assert result.llm_calls == 0


async def test_expired_session_without_operator_fails_like_the_web_surface(live):
    ex, evidence = live("expired")
    result = await Replay(ex).run(savings_capability(), {"member_id": "00123"})
    assert result.status == "failure", result
    assert result.failure.code == "operator_unavailable"
    assert result.failure.intervention_id
    assert result.failure.evidence
    assert result.failure.screenshot == "failure-masked.txt"
    masked = (evidence.directory / result.failure.screenshot).read_text()
    assert not any(ch.isalnum() for ch in masked)


async def test_wrong_member_checkpoint_failure(live):
    ex, _ = live("wrong_member")
    result = await Replay(ex).run(savings_capability(), {"member_id": "00123"})
    assert result.status == "failure", result
    assert result.failure.code == "checkpoint_failed"
    assert result.failure.target == "member_identity"


async def test_ambiguous_and_missing_targets(live):
    ex, _ = live("duplicate")
    surface = ex.surface
    await surface.navigate()
    with pytest.raises(Stop, match="ambiguous_target"):
        surface.count("search")
    with pytest.raises(Stop, match="ambiguous_target"):
        surface._unique("search")
    assert surface.count("savings") == 0
    with pytest.raises(Stop, match="target_missing"):
        surface._unique("savings")


async def test_policy_refuses_a_human_only_control(live):
    ex, _ = live()
    with pytest.raises(Stop, match="human_required"):
        await ex.surface.perform(
            Click(target="renew_session", after=Condition(target="accounts_screen")), {}
        )


async def test_ownership_conflict(live):
    ex, _ = live()
    ex.surface.ownership.owner = "human"
    with pytest.raises(Stop, match="ownership_conflict"):
        await ex.surface.navigate()


async def test_adapter_rejects_unknown_step_type(live):
    ex, evidence = live()
    await ex.surface.navigate()
    unknown = SimpleNamespace(op="fill", target="member_input", input="member_id")
    with pytest.raises(Stop, match="unsupported_action"):
        await ex.surface.perform(unknown, {"member_id": "00123"})
    assert "action_completed" not in (evidence.directory / "events.jsonl").read_text()


async def test_check_health_after_close(live):
    ex, _ = live()
    ex.surface._closed = True
    with pytest.raises(Stop, match="session_closed"):
        ex.surface.check_health()


async def test_absent_and_zero_match_conditions(live):
    ex, _ = live()
    surface = ex.surface
    await surface.navigate()
    assert await surface.condition(Condition(target="savings", kind="absent"), {}) is True
    assert await surface.condition(Condition(target="search", kind="absent"), {}) is False
    assert (
        await surface.condition(
            Condition(target="balance", kind="equals_input", input="member_id"),
            {"member_id": "00123"},
        )
        is False
    )


async def test_masked_snapshot_hides_every_letter_and_digit(live):
    ex, evidence = live()
    surface = ex.surface
    await surface.navigate()
    await surface.perform(Fill(target="member_input", input="member_id"), {"member_id": "00123"})
    name = await surface.failure_screenshot()
    text = (evidence.directory / name).read_text()
    assert "00123" not in text
    assert "Member" not in text
    assert not any(ch.isalnum() for ch in text)
    assert "[" in text and "]" in text  # layout survives the mask


def test_unknown_scenario_is_rejected():
    with pytest.raises(ValueError, match="unknown terminal scenario"):
        TextWorkstation("bogus")


def test_activate_ignores_a_command_name_it_does_not_recognize():
    # ScreenSurface.perform() only ever calls activate() with a name it just
    # resolved via entries(), so this never happens through the real Surface
    # path -- it only isolates the state machine's own fallthrough.
    workstation = TextWorkstation("normal")
    workstation.activate("Not a real command")
    assert workstation.screen == "search"


def test_renew_session_resolves_and_shows_the_account_directory():
    workstation = TextWorkstation("expired")
    workstation.open()
    workstation.set_field("Member identifier", "00123")
    workstation.activate("Find member")
    workstation.activate("Open member")
    workstation.activate("View accounts")
    assert workstation.screen == "expired"
    workstation.activate("Renew session")
    assert workstation.screen == "accounts"
    assert workstation.resolved is True


async def test_reviewed_alternate_rung_is_tried_and_logged(tmp_path):
    # profiles/terminal.json declares no alternates for read_savings, so this
    # exercises the same ladder with a small standalone control -- mirroring
    # BrowserSurface's alternates ladder (REPORT.md §4), never positional or fuzzy.
    control = Control(
        target=Target(kind="screen", role="command", name="Missing button"),
        alternates=(Target(kind="screen", role="command", name="Find member"),),
        operations=("click",),
        description="alternate-ladder probe",
    )
    binding = Binding(
        product="juniper_workstation",
        entry="/",
        routes={"/": ("GET",)},
        controls={"findbtn": control},
        states={},
        input_types={},
        invariants=(Condition(target="findbtn"),),
    )
    evidence = Evidence(tmp_path)
    policy = Policy(binding, "https://juniper-terminal.invalid")
    owner = Ownership(evidence)
    surface = ScreenSurface(policy, owner, evidence, TextWorkstation("normal"))
    await surface.navigate()
    await surface.perform(Click(target="findbtn", after=Condition(target="findbtn")), {})
    rows = [json.loads(x) for x in (evidence.directory / "events.jsonl").read_text().splitlines()]
    assert any(row["event"] == "alternate_resolved" and row["rank"] == 1 for row in rows)


async def test_terminal_fallback_ladder_rescues_a_decoration_only_relabel_via_normalization(
    tmp_path,
):
    # ScreenSurface.find_fallback (mirrors BrowserSurface.find_fallback,
    # REPORT.md §2-4): "FIND MEMBER..." is not the primary "Find member" nor a
    # reviewed alternate, but normalizes identically (case/decoration only) --
    # the ONLY rung this ladder may ever act on, for any op.
    control = Control(
        target=Target(kind="screen", role="command", name="Find member"),
        operations=("click",),
        description="fallback-ladder probe",
    )
    binding = Binding(
        product="juniper_workstation",
        entry="/",
        routes={"/": ("GET",)},
        controls={"findbtn": control},
        states={},
        input_types={},
        invariants=(Condition(target="findbtn"),),
        step_timeout=0.2,
    )
    evidence = Evidence(tmp_path)
    policy = Policy(binding, "https://juniper-terminal.invalid")
    owner = Ownership(evidence)
    surface = ScreenSurface(policy, owner, evidence, TextWorkstation("normal"))
    ex = Execution(surface, policy, evidence, Handoff(owner))
    await surface.navigate()
    surface.workstation.entries = lambda: [Entry("command", "FIND MEMBER...")]
    await ex.settle(Condition(target="findbtn"), {}, 0, acting_op="click")
    rows = [json.loads(x) for x in (evidence.directory / "events.jsonl").read_text().splitlines()]
    resolved = [row for row in rows if row["event"] == "fallback_resolved"]
    assert resolved == [
        {
            "event": "fallback_resolved",
            "target": "findbtn",
            "op": "click",
            "rung": "normalized",
            "sequence": 1,
            "time": resolved[0]["time"],
        }
    ]
    assert ex.fallback_targets["findbtn"] == ("normalized", "click")


async def test_terminal_fallback_ladder_off_reproduces_target_drift(tmp_path):
    control = Control(
        target=Target(kind="screen", role="command", name="Find member"),
        operations=("click",),
        description="fallback-ladder probe",
    )
    binding = Binding(
        product="juniper_workstation",
        entry="/",
        routes={"/": ("GET",)},
        controls={"findbtn": control},
        states={},
        input_types={},
        invariants=(Condition(target="findbtn"),),
        step_timeout=0.2,
        fallback="off",
    )
    evidence = Evidence(tmp_path)
    policy = Policy(binding, "https://juniper-terminal.invalid")
    owner = Ownership(evidence)
    surface = ScreenSurface(policy, owner, evidence, TextWorkstation("normal"))
    ex = Execution(surface, policy, evidence, Handoff(owner))
    await surface.navigate()
    surface.workstation.entries = lambda: [Entry("command", "Find a member")]
    with pytest.raises(Stop, match="target_drift"):
        await ex.settle(Condition(target="findbtn"), {}, 0, acting_op="click")


async def test_terminal_fallback_candidate_owned_by_another_control_is_refused(tmp_path):
    findbtn = Control(
        target=Target(kind="screen", role="command", name="Find member"),
        operations=("click",),
        description="fallback-ladder probe",
    )
    other = Control(
        target=Target(kind="screen", role="command", name="FIND MEMBER..."),
        description="a different reviewed control whose OWN primary is this exact entry",
    )
    binding = Binding(
        product="juniper_workstation",
        entry="/",
        routes={"/": ("GET",)},
        controls={"findbtn": findbtn, "other": other},
        states={},
        input_types={},
        invariants=(Condition(target="findbtn"),),
        step_timeout=0.2,
    )
    evidence = Evidence(tmp_path)
    policy = Policy(binding, "https://juniper-terminal.invalid")
    owner = Ownership(evidence)
    surface = ScreenSurface(policy, owner, evidence, TextWorkstation("normal"))
    ex = Execution(surface, policy, evidence, Handoff(owner))
    await surface.navigate()
    # "FIND MEMBER..." normalizes to findbtn's primary ("Find member"), but it
    # is ALSO exactly control "other"'s own reviewed primary entry -- never
    # stolen, so findbtn still fails closed.
    surface.workstation.entries = lambda: [Entry("command", "FIND MEMBER...")]
    with pytest.raises(Stop, match="target_drift"):
        await ex.settle(Condition(target="findbtn"), {}, 0, acting_op="click")


async def test_scripted_discovery_on_the_terminal_learns_the_same_steps(tmp_path, live):
    ex, _ = live()
    planner = ScriptedPlanner(decisions(savings_capability()))
    artifact, result = await discover(
        ex, planner, TASK, {"member_id": "00123"}, tmp_path / "learned.json"
    )
    assert result.status == "success", result
    assert artifact is not None
    # Surface-independent: the same LOGICAL steps (fill/click/read, the same
    # control keys and learned checkpoints) come out of discovery whether the
    # underlying presentation is a browser page or a text-terminal screen.
    assert artifact.steps == savings_capability().steps
    assert artifact.checkpoint == savings_capability().checkpoint


async def test_read_step_type_check_covers_every_non_click_non_fill_op(live):
    # Read is exercised end to end by the committed-capability replay above;
    # this only isolates perform()'s dispatch for a bare Read call.
    ex, _ = live()
    surface = ex.surface
    await surface.navigate()
    await surface.perform(Fill(target="member_input", input="member_id"), {"member_id": "00123"})
    await surface.perform(Click(target="search", after=Condition(target="results_screen")), {})
    await surface.perform(Click(target="open_member", after=Condition(target="member_screen")), {})
    await surface.perform(Click(target="accounts", after=Condition(target="accounts_screen")), {})
    await surface.perform(Click(target="savings", after=Condition(target="savings_screen")), {})
    value = await surface.perform(Read(target="balance", output="available_balance"), {})
    assert value == "$1,204.57"


def test_the_text_workstation_only_types_into_a_field_that_is_on_screen():
    workstation = TextWorkstation()
    with pytest.raises(KeyError):
        workstation.set_field("Available balance", "1")
    workstation.set_field("Member identifier", "00123")
    workstation.activate("Find member")
    with pytest.raises(KeyError):
        workstation.set_field("Member identifier", "00456")


async def test_terminal_fallback_never_rescues_a_token_superset_near_miss_for_any_op(tmp_path):
    control = Control(
        target=Target(kind="screen", role="field", name="Member id"),
        operations=("read",),
        description="fallback-ladder probe, non-click",
    )
    binding = Binding(
        product="juniper_workstation",
        entry="/",
        routes={"/": ("GET",)},
        controls={"idfield": control},
        states={},
        input_types={},
        invariants=(Condition(target="idfield"),),
        step_timeout=0.2,
    )
    evidence = Evidence(tmp_path)
    policy = Policy(binding, "https://juniper-terminal.invalid")
    owner = Ownership(evidence)
    surface = ScreenSurface(policy, owner, evidence, TextWorkstation("normal"))
    await surface.navigate()
    # "Member id number" merely superset-matches "Member id"'s tokens -- it does
    # NOT normalize equal (a real added word), so it is never a candidate at
    # all, for a "read" op or any other: no rescue, no fallback_resolved event.
    surface.workstation.entries = lambda: [Entry("field", "Member id number", "x")]
    assert await surface.find_fallback("idfield", "read") is None


async def test_terminal_fallback_two_candidates_refuse_as_ambiguous(tmp_path):
    control = Control(
        target=Target(kind="screen", role="command", name="Find member"),
        operations=("click",),
        description="fallback-ladder probe, ambiguous",
    )
    binding = Binding(
        product="juniper_workstation",
        entry="/",
        routes={"/": ("GET",)},
        controls={"findbtn": control},
        states={},
        input_types={},
        invariants=(Condition(target="findbtn"),),
        step_timeout=0.2,
    )
    evidence = Evidence(tmp_path)
    policy = Policy(binding, "https://juniper-terminal.invalid")
    owner = Ownership(evidence)
    surface = ScreenSurface(policy, owner, evidence, TextWorkstation("normal"))
    await surface.navigate()
    surface.workstation.entries = lambda: [
        Entry("command", "Find member:"),
        Entry("command", "Find member…"),
    ]
    with pytest.raises(Stop, match="ambiguous_target"):
        await surface.find_fallback("findbtn", "click")
