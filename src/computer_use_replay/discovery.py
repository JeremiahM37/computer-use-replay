"""LLM chooses; compiler checks references and verifies effects before emitting an artifact."""

from __future__ import annotations

import asyncio
import inspect
import time

from computer_use_replay.contracts import (
    BusinessOutcome,
    Capability,
    Click,
    Condition,
    Fill,
    GoalRequest,
    Grounding,
    Provenance,
    Read,
    Success,
)
from computer_use_replay.engine import POLL_INTERVAL, Execution, Outcome
from computer_use_replay.evidence import Event, Node, Snapshot, atomic_json
from computer_use_replay.policy import Binding, Stop


def _progress_key(entry: dict) -> dict:
    """A decision dict, minus the field the no-progress guard must not compare on.

    A click's `after` starts as the placeholder the model sent (== target) and is
    overwritten with the learned checkpoint once discover() records it in history
    (see the `decision.model_copy(update={"after": ...})` below). Comparing raw
    dicts would therefore never match for a click, however identical the model's
    repeated choice really is.
    """
    if entry.get("op") != "click":
        return entry
    return {k: v for k, v in entry.items() if k != "after"}


async def _surface_live_pin(surface, candidate_id: str):
    """Pin a current live candidate, allowing browser surfaces to be async."""
    pin = getattr(surface, "pin_live_candidate", None)
    if pin is None:
        return
    result = pin(candidate_id)
    if inspect.isawaitable(result):
        result = await result
    if result is False:
        raise Stop(
            "live_candidate_stale",
            "current live candidate",
            "candidate expired",
            target=candidate_id,
        )


async def _surface_live_unpin(surface, candidate_id: str):
    unpin = getattr(surface, "unpin_live_candidate", None)
    if unpin is None:
        return
    result = unpin(candidate_id)
    if inspect.isawaitable(result):
        await result


async def learn_click(ex, step, arguments, index, before, checkpoints=()):
    """Learn a receipt from the effect, without repeating a possibly accepted click."""
    before = await ex.await_effect(
        Condition(target=step.target), arguments, index, acting_op="click"
    )
    pending = [c for c in checkpoints if not await ex.surface.condition(c, arguments)]
    try:
        try:
            await ex.surface.perform(step, arguments)
        except Stop as exc:
            if exc.code != "effect_uncertain":
                raise
    finally:
        ex._unrescue(step.target)
    previous = {node.target for node in before.controls}
    deadline = time.monotonic() + ex.policy.binding.step_timeout
    while True:
        snapshot = await ex.settle(step=index)
        headings = [
            node.target
            for node in snapshot.controls
            if node.target not in previous
            and node.count == 1
            and (
                ex.policy.binding.controls[node.target].target.role == "heading"
                or ex.policy.binding.controls[node.target].checkpoint
            )
        ]
        if len(headings) > 1:
            raise Stop("ambiguous_checkpoint", "one new screen marker", "multiple new markers")
        if headings:
            ex.evidence.emit(Event(event="checkpoint_observed", step=index, target=headings[0]))
            return Click(target=step.target, after=Condition(target=headings[0]))
        changed = [c for c in pending if await ex.surface.condition(c, arguments)]
        if changed:
            # Each candidate is a newly verified caller checkpoint. Prefer an
            # input-bound identity/value over visibility, with stable tie breaking.
            priority = {
                "equals_input": 0,
                "equals_integer_input": 0,
                "count_equals_input": 0,
                "visible": 1,
                "absent": 2,
            }
            condition = min(changed, key=lambda c: (priority[c.kind], c.target))
            ex.evidence.emit(
                Event(event="checkpoint_observed", step=index, target=condition.target)
            )
            return Click(target=step.target, after=condition)
        if time.monotonic() >= deadline:
            raise Stop(
                "checkpoint_failed",
                "new screen marker or changed caller checkpoint",
                "no verified change observed",
                target=step.target,
            )
        await asyncio.sleep(POLL_INTERVAL)


async def _build_planner_context(
    execution: Execution,
    binding: Binding,
    request: GoalRequest,
    arguments: dict,
    snapshot: Snapshot,
    history: list[dict],
    outputs: dict,
) -> tuple[dict, dict[str, Node]]:
    """Build the grounded planner view for one observed browser state."""
    visible = {node.target: node for node in snapshot.controls}
    checkpoint_status = [
        {
            "condition": condition.model_dump(exclude_none=True),
            "satisfied": await execution.surface.condition(condition, arguments),
        }
        for condition in request.checkpoint
    ]
    context = {
        "capability": request.name,
        "goal": request.goal,
        "checkpoint": [condition.model_dump(exclude_none=True) for condition in request.checkpoint],
        "checkpoint_ready": all(item["satisfied"] for item in checkpoint_status),
        "checkpoint_status": checkpoint_status,
        "current_input_matches": {},
        "verified_field_assignments": {},
        "inputs": {key: value.model_dump() for key, value in request.inputs.items()},
        "outputs": {key: value.model_dump() for key, value in request.outputs.items()},
        "observation": snapshot.model_dump(mode="json"),
        "catalog": {
            key: {
                "label": control.target.name,
                "description": control.description,
                "operations": control.operations,
                "risk": control.risk,
                "allowed_inputs": control.allowed_inputs,
            }
            for key, control in binding.controls.items()
            if key in visible
        },
        "history": history,
        "collected_outputs": list(outputs),
        "verified_completed_fields": [],
    }

    live_catalog = {}
    live_input_matches = {}
    live_verified = []
    for candidate in snapshot.live_candidates:
        controls = getattr(execution.surface, "_live_controls", {})
        if candidate.candidate_id not in controls:
            continue
        control = controls[candidate.candidate_id]
        live_catalog[candidate.candidate_id] = {
            "label": candidate.label or "unlabeled live control",
            "description": "live scoped control; model-selected candidate",
            "operations": control.operations,
            "risk": control.risk,
            "allowed_inputs": control.allowed_inputs,
        }
        matches = []
        for input_name in control.allowed_inputs:
            spec = request.inputs.get(input_name)
            if spec is None:
                continue
            kind = "equals_integer_input" if spec.kind == "integer" else "equals_input"
            if await execution.surface.condition(
                Condition(target=candidate.candidate_id, kind=kind, input=input_name),
                arguments,
            ):
                matches.append(input_name)
        # A prior fill is only history. The current live value is the proof;
        # if the application cleared or changed it, the input stays actionable.
        assigned = matches
        live_input_matches[candidate.candidate_id] = assigned or matches
        if assigned and len(control.allowed_inputs) == 1:
            live_catalog[candidate.candidate_id]["allowed_inputs"] = []
            live_verified.append(candidate.candidate_id)
    context["live_catalog"] = live_catalog
    context["live_input_matches"] = live_input_matches
    context["live_verified_fields"] = live_verified

    for key, control in binding.controls.items():
        available_inputs = [
            input_name for input_name in control.allowed_inputs if input_name in arguments
        ]
        if key not in visible or visible[key].count != 1 or not available_inputs:
            continue
        matches = [
            input_name
            for input_name in available_inputs
            if await execution.surface.condition(
                Condition(
                    target=key,
                    kind=(
                        "equals_integer_input"
                        if request.inputs[input_name].kind == "integer"
                        else "equals_input"
                    ),
                    input=input_name,
                ),
                arguments,
            )
        ]
        assigned = [
            input_name
            for input_name in matches
            if any(
                history_item["op"] == "fill"
                and history_item["target"] == key
                and history_item["input"] == input_name
                for history_item in history
            )
        ]
        context["current_input_matches"][key] = matches
        context["verified_field_assignments"][key] = assigned
        if assigned:
            context["catalog"][key]["description"] += (
                "; current value verified from input " + ", ".join(assigned)
            )
        # A reusable editor accepts alternatives, never simultaneous values.
        if len(available_inputs) > 1:
            continue
        context["catalog"][key]["allowed_inputs"] = [] if assigned else available_inputs
        if not assigned:
            context["checkpoint_ready"] = False
        else:
            context["verified_completed_fields"].append(key)
            context["catalog"][key]["description"] += (
                "; verified to already contain the requested value"
            )
            context["catalog"][key]["operations"] = [
                operation for operation in control.operations if operation != "fill"
            ]

    return context, visible


def _compile_decision(
    execution: Execution, request: GoalRequest, binding: Binding, decision, outputs: dict
) -> Fill | Click | Read:
    """Validate a grounded planner decision and turn it into an executable step."""
    if decision.op == "fill":
        if decision.input not in request.inputs:
            raise Stop("undeclared_input")
        controls = getattr(execution.surface, "_live_controls", {})
        if decision.target in controls:
            scope = execution.surface._live_scopes[decision.target][0]
            execution.policy.check_live_action("fill", scope)
            if decision.input not in controls[decision.target].allowed_inputs:
                raise Stop("input_target_mismatch")
        else:
            execution.policy.check_fill(decision.target, decision.input)
        return Fill(target=decision.target, input=decision.input)
    if decision.op == "click":
        if decision.after not in binding.controls and decision.after not in getattr(
            execution.surface, "_live_controls", {}
        ):
            raise Stop("undeclared_checkpoint")
        return Click(target=decision.target, after=Condition(target=decision.after))
    if decision.output not in request.outputs or decision.output in outputs:
        raise Stop("undeclared_or_duplicate_output")
    if request.outputs[decision.output].source != decision.target:
        raise Stop("output_source_mismatch")
    return Read(target=decision.target, output=decision.output)


async def discover(ex: Execution, planner, request, arguments, artifact_path, *, accepted_by=None):
    steps = []
    outputs = {}
    index = None
    history = []
    binding = ex.policy.binding
    try:
        ex.policy.check_request(request)
        ex.handoff.ownership.capability = request.name
        if getattr(ex.surface, "present", False):
            ex.surface.presentation_context(request.name)
        try:
            arguments = request.arguments(arguments)
        except ValueError:
            raise Stop("invalid_input") from None
        ex.evidence.emit(Event(event="started", model=planner.model, llm_calls=0))
        async with asyncio.timeout(binding.run_timeout):
            ex.surface.bind_arguments(arguments)
            await ex.surface.navigate()
            for index in range(binding.max_steps):
                snapshot = await ex.settle(step=index)
                ex.evidence.emit(Event(event="observation", step=index))
                ex.evidence.snapshot(snapshot)
                context, visible = await _build_planner_context(
                    ex, binding, request, arguments, snapshot, history, outputs
                )
                try:
                    decision = await planner.decide(context)
                except Stop as exc:
                    if ex.handoff.operator is None:
                        raise
                    await ex.handoff.intervene(exc.code, index, ex.resumable)
                    continue
                ex.evidence.emit(
                    Event(
                        event="decision",
                        step=index,
                        op=decision.op,
                        target=decision.target,
                        parameter=decision.input,
                        code=decision.reason,
                    )
                )
                if getattr(ex.surface, "present", False):
                    ex.surface.presentation_step(index)
                    if decision.target in binding.controls:
                        label = binding.controls[decision.target].target.name
                        ex.surface.note_decision(
                            f"model chose: {decision.op} {label} (call {planner.calls})"
                        )
                if outputs and decision.op not in {"read", "done", "stop"}:
                    raise Stop(
                        "output_order",
                        "outputs collected after final action",
                        "action after output",
                    )
                if decision.op == "stop":
                    if ex.handoff.operator is None:
                        raise Stop("model_stuck")
                    await ex.handoff.intervene("model_stuck", index, ex.resumable)
                    continue
                if decision.op == "done":
                    if set(outputs) != set(request.outputs) or not steps:
                        raise Stop("false_completion", "all declared outputs", "outputs incomplete")
                    await ex.settle(request.checkpoint, arguments, index)
                    break
                current_live_ids = {
                    candidate.candidate_id for candidate in snapshot.live_candidates
                }
                live_target = decision.target in current_live_ids
                if decision.target not in visible and not live_target:
                    raise Stop(
                        "ungrounded_target", "currently visible control", "unobserved target"
                    )
                if not live_target and visible[decision.target].count != 1:
                    raise Stop("ambiguous_target")
                if live_target:
                    ex.policy.check_live_action(
                        decision.op, ex.surface._live_scopes[decision.target][0]
                    )
                else:
                    ex.policy.check_action(decision.op, decision.target)
                if live_target:
                    await _surface_live_pin(ex.surface, decision.target)
                if not await ex.surface.condition(Condition(target=decision.target), arguments):
                    if live_target:
                        await _surface_live_unpin(ex.surface, decision.target)
                    ex.evidence.emit(
                        Event(
                            event="recovery",
                            step=index,
                            target=decision.target,
                            code="surface_changed_before_action",
                        )
                    )
                    continue
                # Reobserve a stale target before interpreting its now-obsolete arguments.
                step = _compile_decision(ex, request, binding, decision, outputs)
                # A click's `after` is learned from the observed effect, not chosen by the
                # model (the real planner always sends after==target as a placeholder --
                # see planner.parse_call), and history stores the learned value once known
                # below. Comparing raw decisions verbatim can therefore never match a
                # history entry for a click, so a stuck model repeating the identical click
                # silently burns the whole step budget. Drop `after` for clicks so a click
                # repeated at the same target is still recognized as no progress.
                if len(history) >= 2 and all(
                    _progress_key(h) == _progress_key(decision.model_dump()) for h in history[-2:]
                ):
                    raise Stop("no_progress", "new action or completion", "repeated decision")
                if isinstance(step, Click):
                    step = await learn_click(
                        ex, step, arguments, index, snapshot, request.checkpoint
                    )
                    decision = decision.model_copy(update={"after": step.after.target})
                    value = None
                else:
                    if isinstance(step, Read):
                        await ex.settle(request.checkpoint, arguments, index)
                    value = await ex.step(step, arguments, index)
                if live_target:
                    await _surface_live_unpin(ex.surface, decision.target)
                if isinstance(step, Read):
                    try:
                        outputs[step.output] = request.outputs[step.output].parse(value)
                    except ValueError:
                        raise Stop("output_type_mismatch") from None
                steps.append(step)
                history.append(decision.model_dump())
                # Completion is a verified contract fact, not another model prediction.
                if set(outputs) == set(request.outputs):
                    ready = [await ex.surface.condition(c, arguments) for c in request.checkpoint]
                    if all(ready):
                        ex.evidence.emit(Event(event="completion_verified", step=index))
                        break
            else:
                raise Stop("step_budget_exhausted")
        # Only declared session-recovery operations may be omitted from the learned sequence.
        # Manual business steps are not represented in the recorded capability.
        allowed_manual = {
            state.manual_recovery for state in binding.states.values() if state.manual_recovery
        }
        if (
            ex.handoff.ownership.unmapped_manual_actions
            or ex.handoff.ownership.manual_targets - allowed_manual
        ):
            raise Stop(
                "manual_flow_requires_recording",
                "replayable recorded actions",
                "manual business step not compiled",
            )
        used_targets = (
            {step.target for step in steps}
            | {step.after.target for step in steps if isinstance(step, Click)}
            | {
                binding.controls[step.target].after_fill.target
                for step in steps
                if isinstance(step, Fill)
                and step.target in binding.controls
                and binding.controls[step.target].after_fill
            }
            | {condition.target for condition in request.checkpoint}
        )
        grounded = {}
        for key in {step.target for step in steps}:
            if key in getattr(ex.surface, "_live_history", {}):
                _control, metadata = ex.surface._live_history[key]
                scope, source, robustness, role, frames = metadata
                grounded[key] = Grounding(
                    scope=scope,
                    operation=next(step.op for step in steps if step.target == key),
                    target=_control.target,
                    role=role,
                    frame=frames,
                    label_source=source,
                    robustness=robustness,
                )
        artifact = Capability(
            schema_version="3.0" if grounded else "2.0",
            name=request.name,
            product=binding.product,
            binding_sha256=binding.digest(),
            targets={k: v.target for k, v in binding.controls.items() if k in used_targets}
            | {key: value.target for key, value in grounded.items()},
            inputs=request.inputs,
            outputs=request.outputs,
            steps=tuple(steps),
            checkpoint=request.checkpoint,
            grounded=grounded,
            provenance=Provenance(
                mode=planner.mode,
                model=planner.model,
                calls=planner.calls,
                run_id=ex.evidence.run_id,
                accepted_by=accepted_by,
            ),
        )
        ex.policy.check_artifact(artifact)
        atomic_json(artifact_path, artifact.model_dump(mode="json"))
        ex.evidence.emit(
            Event(
                event="artifact_saved", artifact_sha256=artifact.digest(), llm_calls=planner.calls
            )
        )
        ex.evidence.emit(Event(event="success", llm_calls=planner.calls))
        result = Success(outputs=outputs, llm_calls=planner.calls)
        ex.write_fallback_review()
        if ex.fallback_targets:
            result = result.model_copy(update={"warnings": ex.fallback_warnings()})
        ex.evidence.result(result)
        return artifact, result
    except Outcome as outcome:
        ex.evidence.emit(
            Event(event="business_outcome", code=outcome.code, llm_calls=planner.calls)
        )
        result = BusinessOutcome(code=outcome.code, llm_calls=planner.calls)
        ex.write_fallback_review()
        ex.evidence.result(result)
        return None, result
    except Stop as exc:
        stop = exc
    except TimeoutError:
        stop = Stop("discovery_timeout")
    except Exception:
        stop = Stop("discovery_error", "valid provider and surface", "operation failed")
    result = await ex.fail(stop, index, planner.calls)
    ex.write_fallback_review()
    ex.evidence.result(result)
    return None, result
