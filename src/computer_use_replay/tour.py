"""Shared demo/tour scenario harness.

One place builds an in-process fixture session (product profile + a fresh
`serve_demo()` instance + a real `BrowserSurface`/`Execution`), used by both
`computer-use-replay demo` (this module's `run_demo_cli`) and
`scripts/capture_evidence.py`, which only differs in what it does with the
session once it has one. No duplicate copy of the session wiring lives in the
script any more.

`run_tour()` walks the committed `capabilities/*.json` artifacts through the
same eight-scene tour the CLI's `demo` command prints: two ordinary lookups, a
business outcome, a scripted-operator session recovery, a tenant-B replay of
the same artifact, a sub-account preparation stopped at confirmation, the same
`read_savings` artifact once more on the text-terminal surface
(`terminal_session()` + `ScreenSurface`, see REPORT.md §4), and a decoration-
only, unreviewed relabel with no overlay rescued by the verified locator
fallback ladder's normalization-only rung (see REPORT.md §2-4 and
`docs/DESIGN_CHOICES.md`). Every browser step is zero model calls unless
`--discover` learned a fresh `read_savings` artifact first, in which case
that one is used in its place; the terminal step always replays it, never
discovers on it.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from computer_use_replay.browser import BrowserSurface
from computer_use_replay.contracts import Capability, GoalRequest
from computer_use_replay.control import Handoff, Ownership
from computer_use_replay.demo import serve_demo
from computer_use_replay.engine import Execution, Replay
from computer_use_replay.evidence import Evidence
from computer_use_replay.policy import Binding, Policy
from computer_use_replay.terminal import ScreenSurface, TextWorkstation

JUNIPER_BINDING = Path("profiles/juniper.json")
TENANT_B_OVERLAY = Path("profiles/tenant_b.json")
TERMINAL_OVERLAY = Path("profiles/terminal.json")


@asynccontextmanager
async def session(
    root,
    name,
    scenario="normal",
    tenant_b=False,
    human=False,
    drift=False,
    minor_relabel=False,
    fallback=None,
    **surface_kwargs,
):
    """One fixture server plus one real browser/policy/execution stack, torn
    down together. `human=True` attaches a SCRIPTED same-session operator (not
    a person) that clicks "Renew session" in the same live browser and resumes
    automation -- see evidence/README.md for why that is still a genuine
    handoff. `drift=True` renames a live control after every navigation, for
    exercising `target_drift`/`presentation_drift` without a second profile.
    `minor_relabel=True` renames the same control to a DECORATION-only variant
    instead ("Find member" -> "FIND MEMBER..."), for the verified fallback
    ladder's own demo scene -- no overlay, no reviewed alternate, just a
    case/decoration-only relabel the ladder's `normalized` rung resolves and
    logs; a broader relabel (an added or changed word) is deliberately never
    something this ladder acts on. `fallback` overrides the binding's own
    "verified"/"off" setting when not None.
    """
    binding = Binding.load(JUNIPER_BINDING)
    if tenant_b:
        binding = binding.overlay(TENANT_B_OVERLAY)
    if fallback is not None:
        binding = binding.model_copy(update={"fallback": fallback})
    async with serve_demo(scenario) as (origin, app):
        evidence = Evidence(root, name)
        owner = Ownership(evidence)
        policy = Policy(binding, origin)
        async with BrowserSurface(policy, owner, evidence, **surface_kwargs) as surface:

            async def operator(ownership, request, validate):
                lease = await ownership.claim(request)
                frame = surface.page.frame(name="workbench")
                await frame.get_by_role("button", name="Renew session", exact=True).click()
                await frame.get_by_role("heading", name="Account directory", exact=True).wait_for()
                await ownership.resume(lease, validate)

            # Read by engine.settle()'s presentation caption: this operator is the
            # tour's own scripted same-session recovery, never a person.
            operator.scripted = True

            if drift:
                navigate = surface.navigate

                async def rename_button():
                    await navigate()
                    await (
                        surface.page.frame(name="workbench")
                        .get_by_role("button", name="Find member", exact=True)
                        .evaluate("el=>el.textContent='Search member'")
                    )

                surface.navigate = rename_button

            if minor_relabel:
                navigate = surface.navigate

                async def relabel_button():
                    await navigate()
                    await (
                        surface.page.frame(name="workbench")
                        .get_by_role("button", name="Find member", exact=True)
                        .evaluate("el=>el.textContent='FIND MEMBER…'")
                    )

                surface.navigate = relabel_button

            yield (
                Execution(surface, policy, evidence, Handoff(owner, operator if human else None)),
                app,
            )


@asynccontextmanager
async def terminal_session(root, name, scenario="normal", fallback=None):
    """The same product/policy, replayed on the second, text-terminal surface
    (`ScreenSurface` + `profiles/terminal.json`) instead of a browser -- see
    REPORT.md §4. No fixture server, no Playwright: `TextWorkstation` is
    in-process, so this tears down as soon as the `with` block exits.
    """
    binding = Binding.load(JUNIPER_BINDING).overlay(TERMINAL_OVERLAY)
    if fallback is not None:
        binding = binding.model_copy(update={"fallback": fallback})
    evidence = Evidence(root, name)
    owner = Ownership(evidence)
    policy = Policy(binding, "https://juniper-terminal.invalid")
    surface = ScreenSurface(policy, owner, evidence, TextWorkstation(scenario))
    yield Execution(surface, policy, evidence, Handoff(owner)), None


@dataclass
class TourStep:
    id: str
    capability: str
    what: str
    ok: bool
    status: str
    detail: str
    model_calls: int
    seconds: float


@dataclass
class TourReport:
    discovery: dict | None
    steps: list[TourStep] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(step.ok for step in self.steps)

    def to_json(self) -> dict:
        return {
            "discovery": self.discovery,
            "ok": self.ok,
            "steps": [
                {
                    "step": step.id,
                    "capability": step.capability,
                    "what": step.what,
                    "ok": step.ok,
                    "status": step.status,
                    "detail": step.detail,
                    "model_calls": step.model_calls,
                    "seconds": round(step.seconds, 3),
                }
                for step in self.steps
            ],
        }


async def _discover_read_savings(root, planner_factory, present, pace):
    """A genuine discovery of read_savings, using the caller's planner. Returns
    (artifact, summary_dict) -- never touches the committed capabilities/ file.
    """
    request = GoalRequest.load(Path("requests/read_savings.json"))
    async with session(root, "discover_read_savings", present=present, pace=pace) as (ex, _app):
        from computer_use_replay.discovery import discover

        planner = planner_factory(ex.evidence)
        artifact, result = await discover(
            ex, planner, request, {"member_id": "00123"}, root / "learned_read_savings.json"
        )
        if artifact is None:
            raise RuntimeError(
                f"Discovery failed: {getattr(result, 'code', None) or result.failure.code}"
            )
        summary = {
            "model": planner.model,
            "calls": planner.calls,
            "steps": [
                f"{step.op}:{step.target}" + (f"->{step.output}" if step.op == "read" else "")
                for step in artifact.steps
            ],
        }
        return artifact, summary


def expect_success(outputs):
    """A tour-step expectation: exact success with the exact typed outputs."""

    def check(_ex, _app, result):
        if result.status != "success":
            return False, f"expected success, got {result.status}"
        if result.outputs != outputs:
            return False, "unexpected outputs"
        return True, "success"

    return check


def expect_outcome(code):
    """A tour-step expectation: exactly this declared business-outcome code."""

    def check(_ex, _app, result):
        if result.status != "business_outcome" or result.code != code:
            return False, f"expected business_outcome {code}, got {result.status}"
        return True, f"business outcome: {code}"

    return check


def expect_drift(outputs):
    """A tour-step expectation: success with these outputs, plus exactly one
    logged `presentation_drift` event naming at least one target -- the tenant B
    scene's evidence that the same saved artifact tolerated a relabeled surface.
    """

    def check(ex, _app, result):
        if result.status != "success" or result.outputs != outputs:
            return False, f"expected success, got {result.status}"
        events = [
            json.loads(line)
            for line in (ex.evidence.directory / "events.jsonl").read_text().splitlines()
        ]
        drift = [row for row in events if row["event"] == "presentation_drift"]
        if len(drift) != 1 or not drift[0]["targets"]:
            return False, "expected one presentation_drift event"
        return True, f"success + presentation_drift ({drift[0]['count']} targets)"

    return check


def expect_fallback(outputs):
    """A tour-step expectation: success with these outputs, plus exactly one
    logged `fallback_resolved` event and the matching entry in the result's
    `warnings` -- the minor-relabel scene's evidence that the verified ladder,
    not a reviewed overlay, rescued the acting step.
    """

    def check(ex, _app, result):
        if result.status != "success" or result.outputs != outputs:
            return False, f"expected success, got {result.status}"
        events = [
            json.loads(line)
            for line in (ex.evidence.directory / "events.jsonl").read_text().splitlines()
        ]
        resolved = [row for row in events if row["event"] == "fallback_resolved"]
        if len(resolved) != 1 or not result.warnings:
            return False, "expected one fallback_resolved event and a warning"
        return True, f"success + fallback_resolved ({resolved[0]['rung']} rung)"

    return check


async def run_tour(
    root: Path, *, discover=False, planner_factory=None, present=False, pace=0.9, fallback=None
):
    """Run the eight-scene tour against the committed capabilities/*.json (or, if
    `discover` is true, a freshly learned read_savings in their place for the
    read_savings scenes). Evidence for every scene lands under `root`, private
    by convention (the CLI defaults it to `runs/demo`). `fallback` overrides
    every scene's verified-ladder setting when not None (e.g. `--fallback off`).
    """
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
    discovery_summary = None
    savings = Capability.model_validate_json(
        await asyncio.to_thread(Path("capabilities/read_savings.json").read_text)
    )
    subaccount = Capability.model_validate_json(
        await asyncio.to_thread(Path("capabilities/prepare_subaccount.json").read_text)
    )
    if discover:
        savings, discovery_summary = await _discover_read_savings(
            root, planner_factory, present, pace
        )

    report = TourReport(discovery=discovery_summary)

    async def run_step(
        step_id, what, capability, name, scenario, values, expect, *, terminal=False, **kwargs
    ):
        start = time.perf_counter()
        factory = (
            terminal_session(root, name, scenario, fallback=fallback)
            if terminal
            else session(
                root, name, scenario, present=present, pace=pace, fallback=fallback, **kwargs
            )
        )
        try:
            async with factory as (ex, app):
                result = await Replay(ex).run(capability, values)
                ok, detail = expect(ex, app, result)
        except Exception as exc:  # noqa: BLE001 - a tour step failing is data, not a crash
            ok, detail, result = False, f"{type(exc).__name__}: {exc}", None
        elapsed = time.perf_counter() - start
        status = result.status if result is not None else "error"
        report.steps.append(
            TourStep(
                id=step_id,
                capability=capability.name,
                what=what,
                ok=ok,
                status=status,
                detail=detail,
                model_calls=result.llm_calls if result is not None else 0,
                seconds=elapsed,
            )
        )

    balance_a = {"available_balance": {"amount": "1204.57", "currency": "USD"}}
    balance_b = {"available_balance": {"amount": "8902.10", "currency": "USD"}}

    await run_step(
        "a",
        "read_savings for one member",
        savings,
        "a_read_member",
        "normal",
        {"member_id": "00123"},
        expect_success(balance_a),
    )
    await run_step(
        "b",
        "the same capability, a different member",
        savings,
        "b_read_other_member",
        "normal",
        {"member_id": "00456"},
        expect_success(balance_b),
    )
    await run_step(
        "c",
        "an unknown member -> business outcome",
        savings,
        "c_unknown_member",
        "normal",
        {"member_id": "00999"},
        expect_outcome("member_not_found"),
    )
    await run_step(
        "d",
        "expired session -> escalation -> SCRIPTED operator renews -> resume -> success",
        savings,
        "d_expired_then_resumed",
        "expired",
        {"member_id": "00123"},
        expect_success(balance_a),
        human=True,
    )
    await run_step(
        "e",
        "the same artifact on tenant B, with its overlay",
        savings,
        "e_tenant_b",
        "tenant_b",
        {"member_id": "00456"},
        expect_drift(balance_b),
        tenant_b=True,
    )
    await run_step(
        "f",
        "prepare_subaccount up to (not past) its confirmation",
        subaccount,
        "f_subaccount_confirmation",
        "normal",
        {"member_id": "00456", "nickname": "Demo buffer"},
        expect_success({"review_status": "Ready for confirmation"}),
    )
    await run_step(
        "g",
        "the same capability on a text-terminal surface",
        savings,
        "g_terminal_savings",
        "normal",
        {"member_id": "00123"},
        expect_drift(balance_a),
        terminal=True,
    )
    await run_step(
        "h",
        "decorated relabel, no overlay: rescued by normalization",
        savings,
        "h_minor_relabel_fallback",
        "normal",
        {"member_id": "00123"},
        expect_fallback(balance_a),
        minor_relabel=True,
    )
    return report


def render_table(report: TourReport) -> str:
    rows = [("step", "what it shows", "result", "model calls", "seconds")]
    for step in report.steps:
        mark = "ok" if step.ok else "FAIL"
        rows.append(
            (
                step.id,
                step.what,
                f"{mark}: {step.detail}",
                str(step.model_calls),
                f"{step.seconds:.2f}",
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines = []
    for index, row in enumerate(rows):
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
        if index == 0:
            lines.append("  ".join("-" * widths[i] for i in range(len(row))))
    return "\n".join(lines)


def _select_provider(args):
    from computer_use_replay.providers import ProviderConfig

    if os.environ.get("OPENAI_API_KEY") and args.model:
        return ProviderConfig.from_env("openai", args.model)
    return ProviderConfig.from_env("ollama", args.model, args.model_url)


async def run_demo_cli(args) -> int:
    """`computer-use-replay demo` entry point. No model call unless --discover; the rest
    always replays the committed capabilities/*.json artifacts.
    """
    from computer_use_replay.planner import ModelPlanner

    planner_factory = None
    if args.discover:
        config = _select_provider(args)
        planner_factory = lambda evidence: ModelPlanner(config, evidence)  # noqa: E731

    # One private directory per invocation: `demo`, `demo --present` and
    # `demo --discover` can be run back to back without clearing anything.
    root = Path(args.evidence) / f"tour-{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid4().hex[:6]}"
    report = await run_tour(
        root,
        discover=args.discover,
        planner_factory=planner_factory,
        present=args.present,
        pace=args.pace,
        fallback=getattr(args, "fallback", None),
    )
    if report.discovery:
        print(
            f"Discovered read_savings in {report.discovery['calls']} model call(s) "
            f"with {report.discovery['model']}: " + " -> ".join(report.discovery["steps"]),
            file=sys.stderr,
        )
    print(render_table(report), file=sys.stderr)
    print(f"Evidence: {root}", file=sys.stderr)
    print(json.dumps(report.to_json(), indent=2))
    return 0 if report.ok else 1
