"""Browser-only live form perception and candidate grounding.

The model receives only the sanitized candidate projection.  Reviewed scope
selectors and replay aliases remain owned by :class:`BrowserSurface`.
"""

import hashlib
import json

from computer_use_replay.contracts import Target
from computer_use_replay.evidence import LiveCandidate
from computer_use_replay.policy import Control, Stop


class LivePerception:
    def __init__(self, surface):
        self.surface = surface

    async def observe(self):
        return await self._observe_atomic()

    async def _observe_atomic(self):
        surface = self.surface
        candidates = []
        surface._live_epoch += 1
        surface._live_controls = {}
        surface._live_scopes = {}
        surface._live_current_ids = set()
        for scope in surface.policy.binding.live_scopes:
            frame = (
                surface.page.main_frame
                if not scope.frames
                else surface._frame(
                    Target(
                        kind="role",
                        role="heading",
                        name="__reviewed_live_scope__",
                        frames=scope.frames,
                    )
                )
            )
            if frame is None or frame.is_detached():
                continue
            loc = frame.locator(scope.container)
            if await loc.count() == 0:
                continue
            valid_form = await loc.evaluate_all(
                """(forms, expected) => forms.every(form => {
                    const action = new URL(expected.action, location.href).href;
                    if (form.action !== action || !expected.methods.includes(form.method.toUpperCase())) return false;
                    return Array.from(form.querySelectorAll('[formaction], [formmethod]')).every(el => {
                        const a = el.hasAttribute('formaction') ? el.formAction : form.action;
                        const m = el.hasAttribute('formmethod') ? el.formMethod : form.method;
                        return a === action && expected.methods.includes(m.toUpperCase());
                    });
                })""",
                {"action": scope.action, "methods": list(scope.methods)},
            )
            if not valid_form:
                continue
            elements = loc.locator("button, a, input, textarea, select, [role='button']").filter(
                visible=True
            )
            records = await elements.evaluate_all(
                """(els, excluded) => els.map((el, index) => {
                  const r = el.getBoundingClientRect();
                  const tag = el.tagName.toLowerCase();
                  const role = el.getAttribute('role') ||
                    (['input','textarea','select'].includes(tag) ? 'field' : tag === 'button' ? 'button' : 'link');
                          const text = node => { if (excluded.some(s => node.matches(s) || node.closest(s))) return ''; const copy=node.cloneNode(true); for (const s of excluded) copy.querySelectorAll(s).forEach(n=>n.remove()); return (copy.textContent||'').trim(); };
                          const label = el.labels?.[0] ? text(el.labels[0]) : el.getAttribute('aria-label') ||
                    ((tag === 'button' || (tag === 'input' && ['submit','button'].includes(el.type))) ? text(el) || el.value : '');
                  const form = el.form || el.closest('form');
                  const enabled = !el.disabled;
                  const ready = enabled && Array.from(form?.elements || []).every(f => !f.willValidate || f.validity.valid);
                  return {index, name:el.getAttribute('name'), tag, role, label:label.trim(), enabled, ready,
                    excluded: excluded.some(s => el.matches(s) || el.closest(s)),
                    digest:[el.tagName,r.x|0,r.y|0,r.width|0,r.height|0].join(':')};
                })""",
                list(scope.excluded),
            )
            count = len(records)
            if len(candidates) + count > surface.policy.binding.live_candidate_limit:
                raise Stop(
                    "live_candidate_limit", "bounded live candidates", "candidate cap exceeded"
                )
            for record in records:
                index, name, role = record["index"], record["name"], record["role"]
                if record["excluded"]:
                    continue
                if role not in {"button", "link", "field", "submit"}:
                    continue
                ready = (
                    record["ready"] if role in {"button", "link", "submit"} else record["enabled"]
                )
                label = record["label"] if scope.static_text else None
                if scope.static_text:
                    if label and any(str(value) in label for value in surface._arguments.values()):
                        label = None
                    if not label or len(label) > 200:
                        label = None
                digest = record["digest"]
                candidate_id = (
                    "live_"
                    + str(surface._live_epoch)
                    + "_"
                    + hashlib.sha256(
                        (scope.name + ":" + digest + ":" + str(index)).encode()
                    ).hexdigest()[:12]
                )
                frames = tuple(scope.frames)
                if label and role in {"button", "link"}:
                    target = Target(
                        kind="role",
                        role="button" if role == "submit" else role,
                        name=label,
                        frames=frames,
                    )
                    robustness, source = "role", "reviewed_static"
                elif label and role == "field":
                    if not name or name not in scope.fields:
                        continue
                    target = Target(
                        kind="css",
                        name=f"{scope.container} [name={json.dumps(name)}]",
                        frames=frames,
                    )
                    robustness, source = "scope_css", "reviewed_static"
                else:
                    if not name or role != "field" or name not in scope.fields:
                        continue
                    target = Target(
                        kind="css",
                        name=f"{scope.container} [name={json.dumps(name)}]",
                        frames=frames,
                    )
                    robustness, source = "scope_css", "structural"
                input_names = scope.fields.get(name or "", ())
                if role == "link":
                    continue
                allowed_ops = ("fill",) if role == "field" else ("click",)
                operations = tuple(
                    op
                    for op in scope.operations
                    if op in allowed_ops and (op == "click" or input_names)
                )
                if not operations:
                    continue
                control = Control(
                    target=target,
                    operations=operations,
                    risk="reversible",
                    description="live scoped control",
                    allowed_inputs=input_names,
                )
                surface._live_controls[candidate_id] = control
                surface._live_scopes[candidate_id] = (scope.name, source, robustness, role, frames)
                surface._live_current_ids.add(candidate_id)
                candidates.append(
                    LiveCandidate(
                        candidate_id=candidate_id,
                        scope=scope.name,
                        kind="field" if role == "field" else "button",
                        role=role,
                        label=label,
                        frame=frames,
                        ready=ready,
                        locator=target,
                    )
                )
        # Replay aliases resolve a saved logical key to exactly one freshly
        # observed target. Discovery aliases preserve the reviewed target across
        # the settle observation that follows an action.
        for key, grounding in surface._replay_groundings.items():
            matches = [
                candidate
                for candidate in candidates
                if candidate.scope == grounding.scope
                and surface._live_controls[candidate.candidate_id].target == grounding.target
            ]
            if len(matches) == 1:
                candidate = matches[0]
                surface._live_controls[key] = surface._live_controls[candidate.candidate_id]
                surface._live_scopes[key] = surface._live_scopes[candidate.candidate_id]
        current_by_target = {
            control.target: (
                candidate.candidate_id,
                control,
                surface._live_scopes[candidate.candidate_id],
            )
            for candidate in candidates
            for control in [surface._live_controls[candidate.candidate_id]]
        }
        for old_id, (old_control, _old_scope) in surface._live_history.items():
            match = current_by_target.get(old_control.target)
            if match is not None:
                _new_id, control, metadata = match
                surface._live_controls[old_id] = control
                surface._live_scopes[old_id] = metadata
        return candidates

    async def validate_action(self, key, op):
        """Revalidate the selected element against the reviewed scope at dispatch."""
        surface = self.surface
        if key not in surface._live_controls:
            raise Stop(
                "live_candidate_stale", "current live candidate", "candidate expired", target=key
            )
        # Observation IDs are ephemeral.  A retained compiler alias is useful for
        # artifact construction, but it must not become an authority for a later
        # action after its one-action pin has been released.  Replay keys are the
        # only durable exception because they are re-grounded against this page.
        if (
            key not in surface._live_current_ids
            and key not in surface._live_pinned_handles
            and key not in surface._replay_groundings
        ):
            raise Stop(
                "live_candidate_stale",
                "current or pinned live candidate",
                "candidate expired",
                target=key,
            )
        metadata = surface._live_scopes[key]
        scope_name, _source, _robustness, role, frames = metadata
        scope = next(
            (item for item in surface.policy.binding.live_scopes if item.name == scope_name), None
        )
        if (
            scope is None
            or op not in scope.operations
            or op not in surface._live_controls[key].operations
        ):
            raise Stop(
                "action_policy", "reviewed live scope and action", "action denied", target=key
            )
        if role == "link":
            raise Stop("action_policy", "native scoped form control", "link denied", target=key)
        target = surface._live_controls[key].target
        loc = surface._locator(target)
        if loc is None or await loc.count() != 1:
            raise Stop(
                "ambiguous_target", "one current scoped control", "stale or duplicate", target=key
            )
        pinned = surface._live_pinned_handles.get(key)
        if pinned is not None and not await loc.evaluate("(el, pinned) => el === pinned", pinned):
            raise Stop(
                "live_candidate_stale", "same selected element", "element replaced", target=key
            )
        frame = surface._frame(target)
        if frame is None:
            raise Stop("frame_missing", "reviewed frame lineage", "frame absent", target=key)
        valid = await loc.evaluate(
            """(el, expected) => {
              const form = el.form || el.closest('form');
              if (!form || !form.matches(expected.container) || !form.contains(el)) return false;
              const action = new URL(expected.action, location.href).href;
              if (form.action !== action || !expected.methods.includes(form.method.toUpperCase())) return false;
              // Empty overrides have browser-defined defaults; they are not absent.
              const a = el.hasAttribute('formaction') ? el.formAction : form.action;
              const m = el.hasAttribute('formmethod') ? el.formMethod : form.method;
              if (a !== action || !expected.methods.includes(m.toUpperCase())) return false;
              return !expected.excluded.some(selector => el.matches(selector) || el.closest(selector));
            }""",
            {
                "container": scope.container,
                "action": scope.action,
                "methods": list(scope.methods),
                "excluded": list(scope.excluded),
            },
        )
        if not valid:
            raise Stop("action_policy", "current reviewed form scope", "scope changed", target=key)
        # An authored human-only or blocked control always wins over a broad
        # live scope when both resolve to the same current DOM node.
        for authored_key, control in surface.policy.binding.controls.items():
            if control.risk == "reversible" or op not in control.operations:
                continue
            authored = surface._locator(control.target, control.match_input)
            if authored is None or await authored.count() != 1:
                continue
            same = await loc.evaluate(
                "(el, other) => el === other",
                await authored.element_handle(),
            )
            if same:
                code = "human_required" if control.risk == "human_only" else "action_policy"
                raise Stop(
                    code,
                    "reviewed authored control policy",
                    "live scope cannot override",
                    target=authored_key,
                )
