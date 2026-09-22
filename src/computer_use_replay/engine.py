"""Deterministic production interpreter. Intentionally cannot import or call a planner."""

from __future__ import annotations

import asyncio
import time

from computer_use_replay.contracts import (
    BusinessOutcome,
    Click,
    Condition,
    Failed,
    Failure,
    Fill,
    Read,
    Success,
)
from computer_use_replay.control import Handoff
from computer_use_replay.evidence import Event, Evidence, Snapshot, atomic_json
from computer_use_replay.policy import Policy, Stop
from computer_use_replay.surface import Surface

# Shared with discovery.py: how often an unresolved condition is re-checked while
# waiting on a bounded deadline. Not itself a timeout.
POLL_INTERVAL = 0.05

# When several declared states are visible at once, KIND decides what happens --
# never the binding's declaration order, which is presentation authoring order and
# not a safety ranking. Never report a business outcome while the app also shows a
# hard failure or needs a person; clear a known interstitial before trusting what's
# behind it. Lower sorts first == handled first. "transient" has no dedicated
# branch below (it falls through to the plain step-timeout wait), so it is last.
_STATE_PRECEDENCE = {"failure": 0, "human": 1, "interstitial": 2, "business": 3, "transient": 4}


class Outcome(Exception):
    def __init__(self, code):
        self.code = code


class Execution:
    def __init__(self, surface: Surface, policy: Policy, evidence: Evidence, handoff: Handoff):
        self.surface, self.policy, self.evidence, self.handoff = surface, policy, evidence, handoff
        self.recoveries = 0
        self.current_target = None
        # Set by Replay.run() for a replayed artifact; stays None during discovery,
        # where there is no recorded hint yet for target_drift to compare against.
        self.artifact = None
        # Verified fallback ladder bookkeeping (see settle()'s `acting_op` gate
        # and _rescue()/_unrescue() below). `fallback_targets` maps a rescued
        # control key to the (rung, op) that rescued it -- never the observed
        # candidate text, which write_fallback_review() never persists either
        # (see fallback_review.json); `_rescued` holds only the control
        # currently swapped in, restored right after its one action completes,
        # so a saved artifact's `targets` always records the reviewed key's
        # ordinary target -- never a fuzzy-matched locator (see step()).
        self.fallback_targets = {}
        self._rescued = {}

    async def resumable(self):
        self.surface.check_health()
        snapshot = await self.surface.observe()
        return not snapshot.states and snapshot.unknown_dialogs == 0

    def _control_label(self, target):
        control = self._rescued.get(target) or self.policy.binding.controls.get(target)
        if control is None:
            control = getattr(self.surface, "_live_controls", {}).get(target)
        if control is None:
            return target
        return control.target.name

    def _snapshot_has_target(self, snapshot, target):
        return (
            target in {node.target for node in snapshot.controls}
            or target
            in {candidate.candidate_id for candidate in getattr(snapshot, "live_candidates", ())}
            or target in getattr(self.surface, "_live_history", {})
        )

    async def _present(self, text):
        # Presentation is adapter-optional: engine.py stays agnostic of any
        # concrete Surface implementing it. Caption-only; no new evidence event.
        present = getattr(self.surface, "present", False)
        if present:
            await self.surface.present_outcome(text)

    @staticmethod
    def _conditions(condition):
        if condition is None:
            return ()
        return (condition,) if isinstance(condition, Condition) else tuple(condition)

    async def _satisfied(self, conditions, arguments):
        for condition in conditions:
            self.current_target = condition.target
            if not await self.surface.condition(condition, arguments):
                return False
        return True

    async def _rescue(self, key, op):
        """Try the verified fallback ladder for `key`'s own acting target: the
        ONLY rescue this may ever perform is a normalization-only match (case,
        Unicode form, whitespace and pure decorative punctuation -- see
        `fallback.names_equal`), never a fuzzy or partial one. Only called
        from settle() below, only for the step's OWN target (`acting_op`
        set), only once its primary and every reviewed alternate already
        have zero visible matches. Returns True and swaps the control's
        active target to the rescued one (restored by _unrescue() right
        after the one action it enables) if a guarded rescue was found.
        """
        if self.policy.binding.fallback != "verified":
            return False
        find = getattr(self.surface, "find_fallback", None)
        if find is None or key not in self.policy.binding.controls:
            return False
        try:
            self.policy.check_action(op, key)
        except Stop:
            return False  # human_only/blocked controls, or an op it never permits, never rescue.
        match = await find(key, op)
        if match is None:
            return False
        candidate, rung = match
        self.fallback_targets.setdefault(key, (rung, op))
        self.evidence.emit(Event(event="fallback_resolved", target=key, rung=rung, op=op))
        control = self.policy.binding.controls[key]
        self._rescued[key] = control
        # Diagnostics must only ever carry the REVIEWED locator: tell the surface which
        # target to serialize for this key while the rescued one is active.
        vars(self.surface).setdefault("reviewed_targets", {})[key] = control.target
        self.policy.binding = self.policy.binding.model_copy(
            update={
                "controls": {
                    **self.policy.binding.controls,
                    key: control.model_copy(update={"target": candidate}),
                }
            }
        )
        return True

    def _unrescue(self, key):
        original = self._rescued.pop(key, None)
        vars(self.surface).get("reviewed_targets", {}).pop(key, None)
        if original is not None:
            self.policy.binding = self.policy.binding.model_copy(
                update={"controls": {**self.policy.binding.controls, key: original}}
            )

    def fallback_warnings(self):
        return tuple(
            f"fallback_resolved:{key}:{rung}"
            for key, (rung, _op) in sorted(self.fallback_targets.items())
        )

    # Fixed, never-changing instruction text: the only prose in fallback_review.json.
    # Never built from, or including, anything a surface observed.
    _REVIEW_INSTRUCTION = (
        "A rescued control's on-screen wording no longer matches its reviewed "
        "target exactly (case/whitespace/decoration aside). Open the application "
        "and, if the current wording is acceptable, add it as a reviewed "
        "alternate for this control."
    )

    def write_fallback_review(self):
        """fallback_review.json: for each rescued control, its own REVIEWED
        target (already present in the profile), the op and rung that
        rescued it, and a fixed instruction -- never the on-screen text a
        surface actually observed, which never leaves the live surface. A
        person reviews this and decides whether to promote the current
        wording into that control's `alternates` by hand; nothing is ever
        written back to any profile automatically.
        """
        if not self.fallback_targets:
            return
        atomic_json(
            self.evidence.directory / "fallback_review.json",
            {
                key: {
                    "op": op,
                    "rung": rung,
                    "target": self.policy.binding.controls[key].target.model_dump(mode="json"),
                    "instruction": self._REVIEW_INSTRUCTION,
                }
                for key, (rung, op) in sorted(self.fallback_targets.items())
            },
        )

    async def settle(self, condition=None, arguments=None, step=None, *, acting_op=None):
        # A lone Condition is a step's own target or a click/fill receipt: exactly the
        # cases target_drift covers. The multi-condition checkpoint group (discovery's
        # or an artifact's `checkpoint`) is a business-outcome gate, not a single
        # control lookup, and always stays checkpoint_failed even at zero matches.
        # `acting_op` is set ONLY by the one call in step() awaiting a step's own
        # acting target (and learn_click()'s discovery-time equivalent) -- it is
        # what gates the verified fallback ladder to that single moment; every
        # other settle()/await_effect() caller (click postconditions, checkpoints,
        # general condition() checks) leaves it None and stays strict.
        single_target = isinstance(condition, Condition)
        conditions = self._conditions(condition)
        if conditions:
            self.current_target = conditions[0].target
        deadline = time.monotonic() + self.policy.binding.step_timeout
        while True:
            self.surface.check_health()
            snapshot = await self.surface.observe()
            if snapshot.unknown_dialogs:
                self.evidence.snapshot(snapshot)
                await self.handoff.intervene("unexpected_dialog", step, self.resumable)
                deadline = time.monotonic() + self.policy.binding.step_timeout
            elif snapshot.states:
                # Never infer outcomes from free page prose. Precedence is by KIND
                # (_STATE_PRECEDENCE), independent of the binding's declaration order;
                # snapshot.states already preserves that order (see BrowserSurface.observe),
                # so a stable min() only lets declaration order break ties within a kind.
                states = self.policy.binding.states
                code = min(snapshot.states, key=lambda c: _STATE_PRECEDENCE[states[c].kind])
                state = states[code]
                if state.kind == "business":
                    await self._present(f"business outcome: {code}")
                    raise Outcome(code)
                if state.kind == "failure":
                    raise Stop(code, "available application", "declared failure state")
                if state.kind == "human":
                    self.evidence.snapshot(snapshot)
                    await self._present(f"paused: {code} — waiting for operator")
                    await self.handoff.intervene(code, step, self.resumable)
                    scripted = getattr(self.handoff.operator, "scripted", False)
                    await self._present(
                        "operator (scripted) renewed the session"
                        if scripted
                        else "operator resumed"
                    )
                    deadline = time.monotonic() + self.policy.binding.step_timeout
                elif state.kind == "interstitial":
                    self.recoveries += 1
                    if self.recoveries > self.policy.binding.max_recoveries:
                        raise Stop("recovery_exhausted")
                    self.evidence.emit(
                        Event(event="recovery", code=code, step=step, target=state.recovery)
                    )
                    await self.surface.perform(
                        Click(
                            target=state.recovery,
                            after=Condition(target=state.target, kind="absent"),
                        ),
                        {},
                    )
                    while not await self.surface.condition(
                        Condition(target=state.target, kind="absent"), {}
                    ):
                        if time.monotonic() >= deadline:
                            raise Stop("recovery_effect_uncertain")
                        await asyncio.sleep(POLL_INTERVAL)
                    # A successful recovery earns the caller's real awaited condition a
                    # fresh step window, same as the dialog/human branches above --
                    # otherwise a recovery that legitimately used most of the original
                    # budget leaves almost no time to observe what it was recovering
                    # for, and a genuinely successful recovery is immediately followed
                    # by a spurious failure. max_recoveries still bounds total work.
                    deadline = time.monotonic() + self.policy.binding.step_timeout
                elif time.monotonic() >= deadline:
                    raise Stop("load_timeout", "transient load settled", "loading remains")
            elif not conditions or await self._satisfied(conditions, arguments or {}):
                return snapshot
            if time.monotonic() >= deadline:
                if single_target and not self._snapshot_has_target(snapshot, self.current_target):
                    if acting_op and await self._rescue(self.current_target, acting_op):
                        deadline = time.monotonic() + self.policy.binding.step_timeout
                        continue
                    raise Stop(
                        "target_drift",
                        self.current_target,
                        "no matching control in the current presentation",
                        target=self.current_target,
                    )
                raise Stop(
                    "checkpoint_failed",
                    self.current_target if conditions else "ready surface",
                    "condition not satisfied",
                    target=self.current_target if conditions else None,
                )
            await asyncio.sleep(POLL_INTERVAL)

    async def await_effect(self, condition, arguments, index, *, acting_op=None):
        try:
            return await self.settle(condition, arguments, index, acting_op=acting_op)
        except Stop as exc:
            if not self.handoff.operator or exc.code not in {
                "checkpoint_failed",
                "target_missing",
                "target_drift",
                "load_timeout",
                "service_unavailable",
            }:
                raise

            async def restored():
                return await self.resumable() and await self._satisfied(
                    self._conditions(condition), arguments
                )

            self.evidence.snapshot(
                getattr(self.surface, "last_snapshot", Snapshot(controls=(), states=()))
            )
            await self.handoff.intervene(exc.code, index, restored)

    async def step(self, step, arguments, index):
        if getattr(self.surface, "present", False):
            self.surface.presentation_step(index)
        # A rescued target is active only for the wait that finds it and the ONE action it
        # enables. It is restored before anything else happens -- re-observing an uncertain
        # click, escalating, or writing diagnostics -- so evidence and saved artifacts only
        # ever carry the reviewed target, never an on-screen label.
        uncertain = False
        try:
            await self.await_effect(
                Condition(target=step.target), arguments, index, acting_op=step.op
            )
            try:
                value = await self.surface.perform(step, arguments)
            except Stop as exc:
                if exc.code != "effect_uncertain" or not isinstance(step, Click):
                    raise
                uncertain = True
        finally:
            self._unrescue(step.target)
        if uncertain:
            # Re-observe the effect, NEVER repeat the click. A missing receipt escalates.
            await self.await_effect(step.after, arguments, index)
            self.evidence.emit(Event(event="recovery", code="effect_confirmed", step=index))
            await self._present(f"verified: {self._control_label(step.after.target)}")
            return None
        if isinstance(step, Click):
            await self.await_effect(step.after, arguments, index)
            await self._present(f"verified: {self._control_label(step.after.target)}")
        elif (
            isinstance(step, Fill)
            and step.target in self.policy.binding.controls
            and self.policy.binding.controls[step.target].after_fill
        ):
            receipt = self.policy.binding.controls[step.target].after_fill
            await self.await_effect(receipt, arguments, index)
            await self._present(f"verified: {self._control_label(receipt.target)}")
        else:
            await self.settle(step=index)
        return value

    async def fail(self, stop, step, llm_calls=0):
        if stop.code == "invalid_input":
            self.evidence.emit(Event(event="failure", code=stop.code, llm_calls=llm_calls))
            return Failed(
                failure=Failure(code=stop.code, expected=stop.expected, observed=stop.observed),
                llm_calls=llm_calls,
            )
        current = True
        try:
            snapshot = await self.surface.observe()
        except Exception:
            current = False
            snapshot = getattr(self.surface, "last_snapshot", Snapshot(controls=(), states=()))
        path = self.evidence.snapshot(snapshot)
        target = stop.target or self.current_target
        control = self._rescued.get(target) or self.policy.binding.controls.get(target)
        if control is None:
            control = getattr(self.surface, "_live_controls", {}).get(target)
        match_count = (
            next((node.count for node in snapshot.controls if node.target == target), 0)
            if control and current
            else None
        )
        if control and current and target not in {node.target for node in snapshot.controls}:
            match_count = sum(
                candidate.candidate_id == target
                for candidate in getattr(snapshot, "live_candidates", ())
            )
        try:
            screenshot = await self.surface.failure_screenshot()
        except Exception:
            screenshot = None  # A closed/faulted renderer cannot supply a current image.

        hint_differs = None
        if stop.code == "target_drift" and self.artifact is not None:
            hint_differs = target in self.policy.presentation_drift(self.artifact)

        if self.handoff.ownership.owner == "automation":
            await self.handoff.ownership.pause(stop.code, step)
        self.evidence.emit(Event(event="failure", code=stop.code, step=step, llm_calls=llm_calls))
        return Failed(
            failure=Failure(
                code=stop.code,
                step=step,
                expected=stop.expected,
                observed=stop.observed,
                hint_differs=hint_differs,
                evidence=path,
                screenshot=screenshot,
                target=target,
                locator=control.target if control else None,
                match_count=match_count,
                intervention_id=self.handoff.ownership.intervention_id,
            ),
            llm_calls=llm_calls,
        )


class Replay:
    def __init__(self, execution: Execution):
        self.execution = execution

    async def run(self, artifact, arguments):
        ex = self.execution
        index = None
        try:
            ex.handoff.ownership.capability = artifact.name
            ex.policy.check_artifact(artifact)
            ex.artifact = artifact
            install = getattr(ex.surface, "install_groundings", None)
            if install is not None:
                install(artifact.grounded)
            if getattr(ex.surface, "present", False):
                ex.surface.presentation_context(artifact.name, len(artifact.steps))
            try:
                arguments = artifact.arguments(arguments)
            except ValueError:
                raise Stop("invalid_input", "declared typed input", "input rejected") from None
            ex.evidence.emit(
                Event(
                    event="started",
                    capability=artifact.name,
                    artifact_sha256=artifact.digest(),
                    llm_calls=0,
                )
            )
            # Informational, success-path signal: which reviewed hints the current
            # presentation no longer matches. Replay itself always resolves targets
            # through the current presentation, not the recorded hint.
            drifted = ex.policy.presentation_drift(artifact)
            if drifted:
                ex.evidence.emit(
                    Event(event="presentation_drift", targets=drifted, count=len(drifted))
                )
                await ex._present(
                    f"presentation drift: {len(drifted)} reviewed hints differ — "
                    "replay uses the current presentation"
                )
            async with asyncio.timeout(ex.policy.binding.run_timeout):
                ex.surface.bind_arguments(arguments)
                await ex.surface.navigate()
                outputs = {}
                for index, step in enumerate(artifact.steps):
                    if isinstance(step, Read):
                        await ex.await_effect(artifact.checkpoint, arguments, index)
                    value = await ex.step(step, arguments, index)
                    if isinstance(step, Read):
                        try:
                            outputs[step.output] = artifact.outputs[step.output].parse(value)
                        except ValueError:
                            raise Stop(
                                "output_type_mismatch", "declared output type", "parse rejected"
                            ) from None
                await ex.await_effect(artifact.checkpoint, arguments, index)
                ex.evidence.emit(Event(event="success", llm_calls=0))
                result = Success(outputs=outputs)
        except Outcome as outcome:
            ex.evidence.emit(Event(event="business_outcome", code=outcome.code, llm_calls=0))
            result = BusinessOutcome(code=outcome.code)
        except TimeoutError:
            result = await ex.fail(Stop("run_timeout"), index)
        except Stop as stop:
            result = await ex.fail(stop, index)
        except Exception:
            # Raw Playwright errors often include input values and page text.
            result = await ex.fail(
                Stop("surface_error", "working surface", "adapter failed"), index
            )
        ex.write_fallback_review()
        if isinstance(result, Success) and ex.fallback_targets:
            result = result.model_copy(update={"warnings": ex.fallback_warnings()})
        ex.evidence.result(result)
        return result
