"""The verified locator fallback ladder (REPORT.md §2-4, docs/DESIGN_CHOICES.md):
only for a step's OWN acting target, only after its primary and every reviewed
alternate have zero visible matches; unique; same kind/role/frame lineage;
never another control's element; NORMALIZATION ONLY -- the `normalized` rung
(case/Unicode/whitespace/decoration-insensitive exact match) is the ONLY rung
that ever acts, for any op. A broader match (an added, removed or changed
word, or a value embedded in the label) never acts; it stops the run as
`target_drift`, exactly like today's strict behavior, and asks a person to
review it. States/postconditions/checkpoints/observe()/condition() stay
strict throughout. See tests/unit/test_fallback.py for the pure normalize
helper and tests/unit/test_terminal.py for the same ladder on the second
(text) surface.
"""

import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from playwright.async_api import Locator
from playwright.async_api import TimeoutError as BrowserTimeout

from computer_use_replay.browser import BrowserSurface
from computer_use_replay.contracts import (
    Capability,
    Click,
    Condition,
    GoalRequest,
    Input,
    Output,
    Provenance,
    Read,
    Target,
)
from computer_use_replay.control import Handoff, Ownership
from computer_use_replay.discovery import discover
from computer_use_replay.engine import Execution, Replay
from computer_use_replay.evidence import Evidence
from computer_use_replay.policy import Binding, Control, Policy, Stop
from tests.support.planner import ScriptedPlanner, decisions

_REVEAL = (
    "onclick=\"document.body.insertAdjacentHTML('beforeend',"
    "'<h1>Done</h1><table><tr><th>Note</th><td>ok</td></tr></table>')\""
)


def _binding(
    *,
    primary="Find member",
    fallback_mode="verified",
    risk="reversible",
    extra=None,
    step_timeout=0.3,
):
    controls = {
        "go": Control(
            target=Target(kind="role", name=primary, role="button"),
            operations=("click",),
            risk=risk,
            description="acting control under test",
        ),
        "done": Control(
            target=Target(kind="role", name="Done", role="heading"),
            description="click postcondition + run checkpoint",
        ),
        "note": Control(
            target=Target(kind="row_value", name="Note"),
            operations=("read",),
            description="output cell",
        ),
    }
    controls.update(extra or {})
    return Binding(
        product="fallback_probe",
        entry="/",
        routes={"/": ("GET",)},
        controls=controls,
        states={},
        input_types={"unused": Input(kind="boolean")},
        invariants=(Condition(target="done"),),
        step_timeout=step_timeout,
        fallback=fallback_mode,
    )


def _capability(binding):
    return Capability(
        name="probe",
        product=binding.product,
        binding_sha256=binding.digest(),
        targets={key: binding.controls[key].target for key in ("go", "done", "note")},
        inputs={"unused": Input(kind="boolean")},
        outputs={"result": Output(kind="text", source="note")},
        steps=(
            Click(target="go", after=Condition(target="done")),
            Read(target="note", output="result"),
        ),
        checkpoint=(Condition(target="done"),),
        provenance=Provenance(mode="test_fixture", model="fixture", calls=0, run_id="test"),
    )


@asynccontextmanager
async def _probe(tmp_path, binding):
    evidence = Evidence(tmp_path)
    policy = Policy(binding, "http://localhost")
    owner = Ownership(evidence)
    async with BrowserSurface(policy, owner, evidence) as surface:
        yield Execution(surface, policy, evidence, Handoff(owner)), surface, evidence


def _events(evidence):
    return [json.loads(x) for x in (evidence.directory / "events.jsonl").read_text().splitlines()]


async def test_normalized_rescue_completes_click_and_read_with_warning_and_review(tmp_path):
    binding = _binding()
    capability = _capability(binding)
    async with _probe(tmp_path, binding) as (ex, surface, evidence):

        async def fake_navigate():
            await surface.page.set_content(f"<button {_REVEAL}>Find member…</button>")

        surface.navigate = fake_navigate
        result = await Replay(ex).run(capability, {"unused": False})
        assert result.status == "success", result
        assert result.outputs == {"result": "ok"}
        assert result.warnings == ("fallback_resolved:go:normalized",)
        resolved = [r for r in _events(evidence) if r["event"] == "fallback_resolved"]
        assert len(resolved) == 1
        assert resolved[0]["target"] == "go" and resolved[0]["op"] == "click"
        assert resolved[0]["rung"] == "normalized"
        review = json.loads((evidence.directory / "fallback_review.json").read_text())
        # ONLY the REVIEWED name may appear here -- never the observed "Find member…" text.
        assert review == {
            "go": {
                "op": "click",
                "rung": "normalized",
                "target": {
                    "frames": [],
                    "kind": "role",
                    "name": "Find member",
                    "role": "button",
                },
                "instruction": Execution._REVIEW_INSTRUCTION,
            }
        }
        # The artifact's own reviewed targets never changed -- the swap is
        # scoped to the one action, never leaked into policy.binding at rest.
        assert ex.policy.binding.controls["go"].target == binding.controls["go"].target


async def test_a_different_verb_never_rescues_and_stays_target_drift(tmp_path):
    # The exact pair REPORT.md and DESIGN_CHOICES.md call out: "Search member"
    # shares one topic word with "Find member" but is a different verb, so it
    # never normalizes equal and stays an ordinary strict target_drift -- see
    # also tests/e2e/test_replay.py::test_missing_label_has_actionable_private_diagnostics
    # for the same non-match against the fixture's own labels.
    binding = _binding()
    capability = _capability(binding)
    async with _probe(tmp_path, binding) as (ex, surface, evidence):

        async def fake_navigate():
            await surface.page.set_content(f"<button {_REVEAL}>Search member</button>")

        surface.navigate = fake_navigate
        result = await Replay(ex).run(capability, {"unused": False})
        assert result.status == "failure", result
        assert result.failure.code == "target_drift"
        assert result.failure.target == "go"


@pytest.mark.parametrize(
    "rogue_label",
    ["Do not Find member", "Find member and delete"],
    ids=["opposite-action", "extra-destructive-action"],
)
async def test_a_normalized_near_miss_never_dispatches_and_stays_target_drift(
    tmp_path, rogue_label
):
    # The exact defect an external review found in real Chromium: these two
    # labels both token-subset match "Find member", so the removed `similar`
    # rung used to click them anyway and a click's own postcondition can
    # confirm the WANTED effect without ever detecting an unwanted extra one.
    # Under normalization-only rescue neither label is even a candidate --
    # the button is never dispatched at all, proven here with its own
    # fixture-side activation counter, not merely inferred from the failure.
    binding = _binding()
    capability = _capability(binding)
    async with _probe(tmp_path, binding) as (ex, surface, evidence):

        async def fake_navigate():
            await surface.page.set_content(
                '<button onclick="window.__rogueActivations='
                f'(window.__rogueActivations||0)+1">{rogue_label}</button>'
            )

        surface.navigate = fake_navigate
        result = await Replay(ex).run(capability, {"unused": False})
        assert result.status == "failure", result
        assert result.failure.code == "target_drift"
        assert result.failure.target == "go"
        assert await surface.page.evaluate("window.__rogueActivations || 0") == 0
        assert not any(row["event"] == "success" for row in _events(evidence))
        assert not any(row["event"] == "fallback_resolved" for row in _events(evidence))
        assert ex.fallback_targets == {}
        assert not (evidence.directory / "fallback_review.json").exists()


async def test_canary_no_observed_candidate_text_leaks_into_evidence_or_the_caller(tmp_path):
    """DEFECT 2's regression, generalized: whatever a surface observed while
    resolving the ladder must never reach a persisted file OR the caller's own
    response (`result.model_dump()`, exactly what the CLI prints to stdout --
    see cli.py). One run with a rogue near-miss control (a member id baked
    into the label, never rescued, never dispatched) and one with a genuinely
    decoration-only relabel (rescued, successful): neither leaks its observed
    text anywhere, success or failure.
    """
    binding = _binding()
    capability = _capability(binding)

    async def run(button_text):
        async with _probe(tmp_path, binding) as (ex, surface, evidence):

            async def fake_navigate():
                await surface.page.set_content(
                    '<button onclick="window.__seen=(window.__seen||0)+1;'
                    "document.body.insertAdjacentHTML('beforeend','<h1>Done</h1>"
                    "<table><tr><th>Note</th><td>ok</td></tr></table>')\">"
                    f"{button_text}</button>"
                )

            surface.navigate = fake_navigate
            result = await Replay(ex).run(capability, {"unused": False})
            activations = await surface.page.evaluate("window.__seen || 0")
            evidence_text = "\n".join(
                p.read_text(errors="replace") for p in evidence.directory.rglob("*") if p.is_file()
            )
            stdout_proxy = json.dumps(result.model_dump(mode="json"))
            return result, activations, evidence_text, stdout_proxy

    rogue, rogue_hits, rogue_evidence, rogue_stdout = await run("Find member 00123")
    assert rogue.status == "failure" and rogue.failure.code == "target_drift"
    assert rogue_hits == 0
    for needle in ("00123", "Find member 00123"):
        assert needle not in rogue_evidence
        assert needle not in rogue_stdout

    rescued, rescued_hits, rescued_evidence, rescued_stdout = await run("FIND MEMBER…")
    assert rescued.status == "success", rescued
    assert rescued_hits == 1
    for needle in ("00123", "Find member 00123", "FIND MEMBER…", "FIND MEMBER"):
        assert needle not in rescued_evidence
        assert needle not in rescued_stdout


async def test_normalized_rescue_also_applies_to_a_non_click_fill_target(tmp_path):
    # The removed `similar` rung used to be click-only; `normalized` was never
    # op-restricted and still is not. This exercises a `label`-kind primary
    # (the candidate query a click-only rung would never reach for a
    # fill-target control) and confirms the op is recorded, not just "click".
    extra = {
        "field": Control(
            target=Target(kind="label", name="Amount to send"),
            operations=("fill",),
            allowed_inputs=("unused",),
            description="fill-target label control",
        )
    }
    binding = _binding(extra=extra)
    evidence = Evidence(tmp_path)
    policy = Policy(binding, "http://localhost")
    owner = Ownership(evidence)
    async with BrowserSurface(policy, owner, evidence) as surface:
        ex = Execution(surface, policy, evidence, Handoff(owner))
        await surface.page.set_content("<label>AMOUNT TO SEND... <input></label>")
        await ex.settle(Condition(target="field"), {}, 0, acting_op="fill")
        resolved = [r for r in _events(evidence) if r["event"] == "fallback_resolved"]
        assert resolved and resolved[0]["target"] == "field"
        assert resolved[0]["rung"] == "normalized" and resolved[0]["op"] == "fill"


async def test_two_normalized_candidates_refuse_as_ambiguous_target(tmp_path):
    binding = _binding()
    evidence = Evidence(tmp_path)
    policy = Policy(binding, "http://localhost")
    owner = Ownership(evidence)
    async with BrowserSurface(policy, owner, evidence) as surface:
        ex = Execution(surface, policy, evidence, Handoff(owner))
        # Both decorate the same words differently; both normalize to "find member".
        await surface.page.set_content("<button>Find member…</button><button>Find member:</button>")
        with pytest.raises(Stop, match="ambiguous_target"):
            await ex.settle(Condition(target="go"), {}, 0, acting_op="click")


async def test_a_normalized_candidate_owned_by_another_control_is_never_stolen(tmp_path):
    extra = {
        "other": Control(
            target=Target(kind="role", name="Find member…", role="button"),
            description="a different reviewed control whose OWN primary is this exact text",
        )
    }
    binding = _binding(extra=extra)
    evidence = Evidence(tmp_path)
    policy = Policy(binding, "http://localhost")
    owner = Ownership(evidence)
    async with BrowserSurface(policy, owner, evidence) as surface:
        ex = Execution(surface, policy, evidence, Handoff(owner))
        # "Find member…" normalizes to "go"'s primary ("Find member"), but it is
        # ALSO exactly control "other"'s own reviewed primary target -- never
        # stolen, so "go" still fails closed.
        await surface.page.set_content("<button>Find member…</button>")
        with pytest.raises(Stop, match="target_drift"):
            await ex.settle(Condition(target="go"), {}, 0, acting_op="click")


async def test_human_only_control_is_never_rescued(tmp_path):
    binding = _binding(risk="human_only")
    evidence = Evidence(tmp_path)
    policy = Policy(binding, "http://localhost")
    owner = Ownership(evidence)
    async with BrowserSurface(policy, owner, evidence) as surface:
        ex = Execution(surface, policy, evidence, Handoff(owner))
        await surface.page.set_content("<button>Find member…</button>")
        with pytest.raises(Stop, match="target_drift"):
            await ex.settle(Condition(target="go"), {}, 0, acting_op="click")
        assert ex.fallback_targets == {}


async def test_fallback_off_reproduces_plain_target_drift(tmp_path):
    binding = _binding(fallback_mode="off")
    evidence = Evidence(tmp_path)
    policy = Policy(binding, "http://localhost")
    owner = Ownership(evidence)
    async with BrowserSurface(policy, owner, evidence) as surface:
        ex = Execution(surface, policy, evidence, Handoff(owner))
        await surface.page.set_content("<button>Find member…</button>")
        with pytest.raises(Stop, match="target_drift"):
            await ex.settle(Condition(target="go"), {}, 0, acting_op="click")
        assert ex.fallback_targets == {}


async def test_states_conditions_and_checkpoints_never_consult_the_ladder(tmp_path, monkeypatch):
    binding = _binding()
    evidence = Evidence(tmp_path)
    policy = Policy(binding, "http://localhost")
    owner = Ownership(evidence)
    async with BrowserSurface(policy, owner, evidence) as surface:
        ex = Execution(surface, policy, evidence, Handoff(owner))
        await surface.page.set_content("<h1>Unrelated</h1>")
        rescued = []

        async def cheating_rescue(key, op):
            # A stub that would ALWAYS "succeed" if it were ever consulted --
            # settle() must never call this without acting_op set.
            rescued.append(key)
            return True

        monkeypatch.setattr(ex, "_rescue", cheating_rescue)
        with pytest.raises(Stop, match="target_drift"):
            await ex.settle(Condition(target="done"), {}, 0)  # acting_op=None: strict
        assert rescued == []


async def test_discovery_rescues_a_control_relabeled_between_decision_and_action(
    live, capability, tmp_path
):
    # The control is genuinely visible and unique when discovery's own
    # pre-action condition() check runs (so it passes the ordinary
    # ungrounded/ambiguous/"surface_changed_before_action" checks discover()
    # already makes); the page is relabeled, with a DECORATION-only variant,
    # as a side effect of that SAME check -- modeling a relabel landing between
    # a model's decision and the actual action. learn_click()'s own
    # await_effect (see discovery.py) then has to use the same acting_op-gated
    # ladder replay uses, and the saved artifact still records the reviewed key's
    # ordinary target, never the fuzzy-matched one that actually got clicked.
    async with live() as (ex, app):
        await ex.surface.navigate()
        original_condition = ex.surface.condition
        relabeled = {"done": False}

        async def relabel_once_after_the_real_check(condition, arguments):
            satisfied = await original_condition(condition, arguments)
            if condition.target == "search" and not relabeled["done"]:
                relabeled["done"] = True
                await (
                    ex.surface.page.frame(name="workbench")
                    .get_by_role("button", name="Find member", exact=True)
                    .evaluate("el => el.textContent = 'FIND MEMBER…'")
                )
            return satisfied

        ex.surface.condition = relabel_once_after_the_real_check
        planner = ScriptedPlanner(decisions(capability))
        request = GoalRequest.load(Path("requests/read_savings.json"))
        artifact, result = await discover(
            ex, planner, request, {"member_id": "00123"}, tmp_path / "learned.json"
        )
        assert result.status == "success", result
        assert relabeled["done"] is True
        assert artifact is not None
        assert app.state.finalizations == 0
        # The saved artifact records the reviewed KEY's ordinary target -- NEVER
        # the fuzzy-matched locator that actually got clicked.
        assert artifact.targets["search"] == ex.policy.binding.controls["search"].target
        assert artifact.targets["search"].name == "Find member"
        assert result.warnings and result.warnings[0].startswith("fallback_resolved:search:")
        rows = _events(ex.evidence)
        assert any(r["event"] == "fallback_resolved" and r["target"] == "search" for r in rows)
        review = json.loads((ex.evidence.directory / "fallback_review.json").read_text())
        # ONLY the REVIEWED name -- never the observed "FIND MEMBER…" text.
        assert review["search"]["target"]["name"] == "Find member"
        assert review["search"]["rung"] == "normalized"
        assert "FIND MEMBER" not in (ex.evidence.directory / "fallback_review.json").read_text()


async def test_fallback_query_returns_none_for_a_kind_it_does_not_enumerate(tmp_path):
    # css/screen never reach _fallback_query (find_fallback returns early for
    # them, see below); this isolates its own defensive fallthrough.
    binding = _binding()
    evidence = Evidence(tmp_path)
    policy = Policy(binding, "http://localhost")
    owner = Ownership(evidence)
    async with BrowserSurface(policy, owner, evidence) as surface:
        await surface.page.set_content("<button>Find member</button>")
        assert surface._fallback_query(surface.page.main_frame, "css", None) is None


async def test_claimed_by_another_control_is_false_when_the_candidate_has_no_box(tmp_path):
    binding = _binding()
    evidence = Evidence(tmp_path)
    policy = Policy(binding, "http://localhost")
    owner = Ownership(evidence)
    async with BrowserSurface(policy, owner, evidence) as surface:
        await surface.page.set_content("<p>empty</p>")
        missing = surface.page.locator("#does-not-exist")
        assert await surface._claimed_by_another_control("go", missing) is False


async def test_find_fallback_returns_none_for_a_css_primary_target(tmp_path):
    # "css targets get no fallback" (REPORT.md §2-4): a css-kind control never
    # even reaches the candidate search.
    extra = {
        "css_control": Control(
            target=Target(kind="css", name="#missing"),
            operations=("click",),
            description="css primary, deliberately unreachable by the ladder",
        )
    }
    binding = _binding(extra=extra)
    evidence = Evidence(tmp_path)
    policy = Policy(binding, "http://localhost")
    owner = Ownership(evidence)
    async with BrowserSurface(policy, owner, evidence) as surface:
        await surface.page.set_content("<button>anything</button>")
        assert await surface.find_fallback("css_control", "click") is None


async def test_find_fallback_returns_none_when_the_frame_lineage_is_absent(tmp_path):
    extra = {
        "framed": Control(
            target=Target(kind="role", name="Go", role="button", frames=("nonexistent",)),
            operations=("click",),
            description="a control whose reviewed frame is not on this page",
        )
    }
    binding = _binding(extra=extra)
    evidence = Evidence(tmp_path)
    policy = Policy(binding, "http://localhost")
    owner = Ownership(evidence)
    async with BrowserSurface(policy, owner, evidence) as surface:
        await surface.page.set_content("<button>Go</button>")
        assert await surface.find_fallback("framed", "click") is None


async def test_find_fallback_returns_none_when_the_matched_name_has_no_unique_locator(
    tmp_path,
):
    # "Amount" matches the row_value primary's normalized rung by header text
    # alone, but that row has no data cell: re-locating it for real yields zero
    # matches, so the ladder must return nothing instead of a broken locator.
    extra = {
        "amount": Control(
            target=Target(kind="row_value", name="Amount"),
            operations=("read",),
            description="row_value primary with no matching value cell",
        )
    }
    binding = _binding(extra=extra)
    evidence = Evidence(tmp_path)
    policy = Policy(binding, "http://localhost")
    owner = Ownership(evidence)
    async with BrowserSurface(policy, owner, evidence) as surface:
        await surface.page.set_content("<table><tr><th>Amount</th></tr></table>")
        assert await surface.find_fallback("amount", "read") is None


async def test_rescue_refuses_when_the_surface_offers_no_find_fallback(tmp_path):
    # Execution._rescue() is surface-agnostic: a Surface implementation with no
    # find_fallback (e.g. a minimal third adapter) simply never rescues, rather
    # than raising AttributeError.
    binding = _binding()
    evidence = Evidence(tmp_path)
    policy = Policy(binding, "http://localhost")
    owner = Ownership(evidence)

    class NoFallbackSurface:
        present = False

        async def condition(self, condition, arguments):
            return False

        async def observe(self):
            from computer_use_replay.evidence import Snapshot

            return Snapshot(controls=(), states=())

        def check_health(self):
            return None

    ex = Execution(NoFallbackSurface(), policy, evidence, Handoff(owner))
    assert await ex._rescue("go", "click") is False
    assert ex.fallback_targets == {}


async def test_rescue_refuses_an_undeclared_control_key(tmp_path):
    binding = _binding()
    evidence = Evidence(tmp_path)
    policy = Policy(binding, "http://localhost")
    owner = Ownership(evidence)
    async with BrowserSurface(policy, owner, evidence) as surface:
        ex = Execution(surface, policy, evidence, Handoff(owner))
        assert await ex._rescue("not_a_real_control", "click") is False


def _evidence_text(evidence):
    return "\n".join(
        path.read_text()
        for path in sorted(evidence.directory.rglob("*"))
        if path.is_file() and path.suffix != ".png"
    )


async def test_an_uncertain_click_after_a_rescue_never_persists_the_on_screen_label(
    tmp_path, monkeypatch
):
    """The decorated label rescues the click; the click then times out after it was
    accepted, its postcondition never appears, and the run fails with diagnostics.
    Every diagnostic written on that path -- the state snapshot, the failure's locator
    -- must carry the REVIEWED locator, never the on-screen "FIND MEMBER…" text."""
    click = Locator.click

    async def accepted_then_uncertain(locator, **kwargs):
        await click(locator, **kwargs)
        raise BrowserTimeout("accepted the click but confirmation timed out")

    monkeypatch.setattr(Locator, "click", accepted_then_uncertain)
    binding = _binding(step_timeout=1.0)
    async with _probe(tmp_path, binding) as (ex, surface, evidence):

        async def fake_navigate():
            await surface.page.set_content(
                '<button onclick="window.clicks=(window.clicks||0)+1">FIND MEMBER…</button>'
            )

        surface.navigate = fake_navigate
        result = await Replay(ex).run(_capability(binding), {"unused": False})
        assert await surface.page.evaluate("window.clicks") == 1  # dispatched once, never repeated
    assert result.status == "failure", result
    # The run stops waiting for the click's own postcondition, so that is the target named.
    assert result.failure.target == "done" and result.failure.locator.name == "Done"
    assert [r["rung"] for r in _events(evidence) if r["event"] == "fallback_resolved"] == [
        "normalized"
    ]
    persisted = _evidence_text(evidence) + result.model_dump_json()
    assert "FIND MEMBER" not in persisted
    assert ex.policy.binding.controls["go"].target == binding.controls["go"].target
    assert surface.reviewed_targets == {}


async def test_a_snapshot_taken_while_a_rescue_is_active_serializes_the_reviewed_locator(tmp_path):
    binding = _binding(step_timeout=1.0)
    async with _probe(tmp_path, binding) as (ex, surface, _evidence):
        await surface.page.set_content("<button>FIND MEMBER…</button>")
        assert await ex._rescue("go", "click")
        try:
            snapshot = await surface.observe()
            node = next(node for node in snapshot.controls if node.target == "go")
            assert node.count == 1
            assert node.locator.name == "Find member"
            assert "FIND MEMBER" not in snapshot.model_dump_json()
            assert ex._control_label("go") == "Find member"
        finally:
            ex._unrescue("go")
        assert surface.reviewed_targets == {}
        assert ex.policy.binding.controls["go"].target == binding.controls["go"].target
