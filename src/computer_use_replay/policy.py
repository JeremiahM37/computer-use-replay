"""Trusted installation binding and current execution policy, separate from recordings."""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import Field, model_serializer, model_validator

from computer_use_replay.contracts import Condition, Input, LiveScope, Name, Strict, Target, digest
from computer_use_replay.network import RequestRule


class Stop(Exception):
    """Only stable codes cross the persistence boundary; never raw library messages."""

    def __init__(
        self,
        code: str,
        expected: str = "permitted UI state",
        observed: str = "blocked",
        *,
        target=None,
    ):
        self.code, self.expected, self.observed = code, expected, observed
        self.target = target
        super().__init__(code)


class Control(Strict):
    target: Target
    # Reviewed fallback rungs, tried in order and ONLY when the primary target has
    # zero visible matches -- never positional, never fuzzy. Every rung, primary or
    # alternate, still requires exactly one visible match; more than one stops with
    # ambiguous_target. Presentation layer only: excluded from Binding.digest() (see
    # digest()) and never copied into a saved artifact's `targets` hints.
    alternates: tuple[Target, ...] = ()
    operations: tuple[Literal["click", "fill", "read"], ...] = ()
    risk: Literal["reversible", "human_only", "blocked"] = "reversible"
    description: str
    checkpoint: bool = False
    after_fill: Condition | None = None
    commit_key: Literal["Tab"] | None = None
    text_mode: Literal["rendered", "paragraphs"] = "rendered"
    match_input: Name | None = None
    allowed_inputs: tuple[Name, ...] = ()
    allowed_output_values: tuple[str, ...] = ()

    @model_serializer(mode="wrap")
    def serialized(self, handler):
        data = handler(self)
        if not self.alternates:
            data.pop("alternates", None)
        if self.match_input is None:
            data.pop("match_input", None)
        if not self.checkpoint:
            data.pop("checkpoint", None)
        if self.after_fill is None:
            data.pop("after_fill", None)
        if self.commit_key is None:
            data.pop("commit_key", None)
        if self.text_mode == "rendered":
            data.pop("text_mode", None)
        return data


class State(Strict):
    target: Name
    kind: Literal["business", "transient", "human", "failure", "interstitial"]
    recovery: Name | None = None
    manual_recovery: Name | None = None

    @model_validator(mode="after")
    def recovery_shape(self):
        if (self.kind == "human") != (self.manual_recovery is not None):
            raise ValueError("human states require a manual recovery control")
        if (self.kind == "interstitial") != (self.recovery is not None):
            raise ValueError("only interstitials require a recovery control")
        return self


class Binding(Strict):
    version: Literal["1.0"] = "1.0"
    product: Name
    entry: str
    routes: dict[str, tuple[Literal["GET", "POST"], ...]]
    request_rules: tuple[RequestRule, ...] = ()
    controls: dict[Name, Control]
    live_mode: Literal["off", "forms"] = "off"
    live_scopes: tuple[LiveScope, ...] = ()
    live_candidate_limit: int = Field(default=64, ge=1, le=256)
    states: dict[Name, State]
    input_types: dict[Name, Input]
    invariants: tuple[Condition, ...] = Field(min_length=1)
    max_steps: int = Field(default=16, ge=1, le=40)
    step_timeout: float = Field(default=3.0, gt=0, le=30)
    run_timeout: float = Field(default=180.0, gt=0, le=600)
    max_recoveries: int = Field(default=2, ge=0, le=5)
    # Presentation-layer setting, same authority boundary as `alternates`:
    # "verified" lets Execution._rescue() try the normalization-only ladder
    # for a step's own acting target once its primary and every reviewed
    # alternate have zero visible matches (see engine.py); "off" reproduces
    # today's strict target_drift. Excluded from digest() below, so neither a
    # profile nor an artifact fingerprint ever changes because of it.
    fallback: Literal["verified", "off"] = "verified"

    @model_serializer(mode="wrap")
    def serialized(self, handler):
        data = handler(self)
        if not self.request_rules:
            data.pop("request_rules", None)
        if self.live_mode == "off":
            data.pop("live_mode", None)
            data.pop("live_scopes", None)
            data.pop("live_candidate_limit", None)
        return data

    @model_validator(mode="after")
    def valid(self):
        if self.live_mode == "forms" and not self.live_scopes:
            raise ValueError("live mode requires reviewed scopes")
        if self.live_mode == "off" and self.live_scopes:
            raise ValueError("live scopes require live mode")
        if len({scope.name for scope in self.live_scopes}) != len(self.live_scopes):
            raise ValueError("duplicate live scope")
        for scope in self.live_scopes:
            if (
                not scope.action.startswith("/")
                or scope.action.startswith("//")
                or any(token in scope.action for token in ("?", "#", "\\"))
                or any(method not in self.routes.get(scope.action, ()) for method in scope.methods)
            ):
                raise ValueError("live scope action must be an allowed relative route")
            for method in scope.methods:
                if method != "POST":
                    continue
                try:
                    rule = Policy(self, "https://binding.invalid").check_url(
                        "https://binding.invalid" + scope.action, method
                    )
                except Stop:
                    rule = None
                if rule is None or rule.discard:
                    raise ValueError("live POST scope requires a non-discard body grant")
                if not set(scope.fields) <= set(rule.body_keys):
                    raise ValueError("live scope fields require declared body grant keys")

        def allowed_get(path):
            try:
                return Policy(self, "https://binding.invalid").check_network_request(
                    "https://binding.invalid" + path, "GET"
                )
            except Stop:
                return False

        if not self.entry.startswith("/") or self.entry.startswith("//"):
            raise ValueError("entry must be a relative allowed GET route")
        if not allowed_get(self.entry):
            raise ValueError("entry must be an allowed GET route")
        for rule in self.request_rules:
            if any(not allowed_get(destination) for destination in rule.redirect_to):
                raise ValueError("redirect destination must itself be an allowed GET route")
        for control in self.controls.values():
            if control.commit_key is not None and "fill" not in control.operations:
                raise ValueError("commit key requires a fill control")
            if control.after_fill:
                if (
                    "fill" not in control.operations
                    or control.after_fill.target not in self.controls
                ):
                    raise ValueError("fill receipt requires a fill control and declared target")
                receipt_control = self.controls[control.after_fill.target]
                if any(
                    name and name not in control.allowed_inputs
                    for name in (control.after_fill.input, receipt_control.match_input)
                ):
                    raise ValueError("fill receipt must use an allowed field input")
            if control.target.scope is not None and not control.match_input:
                raise ValueError("container scopes require a matched input")
            if control.match_input and (
                control.match_input not in self.input_types or control.target.kind != "css"
            ):
                raise ValueError("parameter matching requires a CSS scope and declared input")
            # Every rung obeys the same strict-targeting rules as the primary target.
            for alternate in control.alternates:
                if alternate.scope is not None and not control.match_input:
                    raise ValueError("container scopes require a matched input")
            if bool(control.allowed_inputs) != ("fill" in control.operations):
                raise ValueError("fill controls require explicit input bindings")
            if not set(control.allowed_inputs) <= self.input_types.keys():
                raise ValueError("undefined fill input")
        for scope in self.live_scopes:
            for names in scope.fields.values():
                if not set(names) <= self.input_types.keys():
                    raise ValueError("undefined live input")
        for state in self.states.values():
            if state.target not in self.controls or (
                state.recovery and state.recovery not in self.controls
            ):
                raise ValueError("undefined state control")
            if state.manual_recovery:
                manual = self.controls.get(state.manual_recovery)
                if (
                    manual is None
                    or manual.risk != "human_only"
                    or "click" not in manual.operations
                ):
                    raise ValueError("manual recovery requires a human-only click")
            if state.recovery:
                recovery = self.controls[state.recovery]
                if recovery.risk != "reversible" or "click" not in recovery.operations:
                    raise ValueError("automatic recovery must be a reversible click")
        for condition in self.invariants:
            if condition.target not in self.controls or (
                condition.input and condition.input not in self.input_types
            ):
                raise ValueError("undefined checkpoint reference")
        for condition in (
            *self.invariants,
            *(control.after_fill for control in self.controls.values() if control.after_fill),
        ):
            condition.check_input_type(self.input_types)
        return self

    @classmethod
    def load(cls, path: Path):
        return cls.model_validate_json(path.read_text())

    def digest(self):
        contract = self.model_dump(mode="json")
        # Locators are reviewed tenant presentation. They cannot alter operations/risk/state rules.
        # Alternates are the same presentation layer as the primary target: a reviewed
        # fallback rung never changes what the product permits, so it stays out of the
        # fingerprint too -- adding or reordering alternates never invalidates a saved artifact.
        for control in contract["controls"].values():
            del control["target"]
            control.pop("alternates", None)
        # Same story for the verified-ladder toggle itself: flipping it never
        # changes what the product permits, only how hard replay tries to find
        # a control's own reviewed target.
        contract.pop("fallback", None)
        return digest(contract)

    def overlay(self, path: Path):
        presentation = Presentation.model_validate_json(path.read_text())
        if (
            presentation.product != self.product
            or not presentation.targets.keys() <= self.controls.keys()
            or not presentation.alternates.keys() <= self.controls.keys()
        ):
            raise ValueError("incompatible presentation")
        raw = self.model_dump(mode="json")
        for key, target in presentation.targets.items():
            raw["controls"][key]["target"] = target.model_dump(mode="json")
        for key, alternates in presentation.alternates.items():
            raw["controls"][key]["alternates"] = [t.model_dump(mode="json") for t in alternates]
        for control in raw["controls"].values():
            control["target"]["frames"] = [
                presentation.frames.get(frame, frame) for frame in control["target"]["frames"]
            ]
            for alternate in control.get("alternates", ()):
                alternate["frames"] = [
                    presentation.frames.get(frame, frame) for frame in alternate["frames"]
                ]
        if presentation.fallback is not None:
            raw["fallback"] = presentation.fallback
        return Binding.model_validate(raw)


class Presentation(Strict):
    product: Name
    tenant: Name
    targets: dict[Name, Target] = Field(default_factory=dict)
    frames: dict[Name, Name] = Field(default_factory=dict)
    # Reviewed fallback rungs an overlay adds or replaces for an existing control.
    # Same authority boundary as `targets`: locators only, never actions/risk/checkpoints.
    alternates: dict[Name, tuple[Target, ...]] = Field(default_factory=dict)
    # Same authority boundary again: a tenant overlay may turn the verified
    # ladder off (or back on) without touching product policy. None means
    # "leave the binding's own setting alone".
    fallback: Literal["verified", "off"] | None = None


class Policy:
    def __init__(self, binding: Binding, origin: str):
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise Stop("invalid_origin")
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        self.binding = binding

    def check_url(self, url: str, method: str = "GET"):
        parsed = urlsplit(url)
        if (
            f"{parsed.scheme}://{parsed.netloc}" != self.origin
            or parsed.username
            or parsed.password
            or parsed.fragment
            or unquote(parsed.path) != parsed.path
            or "\\" in parsed.path
            or any(part in {".", ".."} for part in parsed.path.split("/"))
        ):
            raise Stop("network_policy")
        try:
            rules = [
                r
                for r in self.binding.request_rules
                if r.matches(parsed.path, method, parsed.query)
            ]
        except ValueError:
            raise Stop("network_policy") from None
        if len(rules) == 1:
            return rules[0]
        if rules or parsed.query or method not in self.binding.routes.get(parsed.path, ()):
            raise Stop(
                "network_policy",
                "allowlisted origin, route, method and parameters",
                "request blocked",
            )
        return None

    def check_network_request(self, url, method, body=None):
        rule = self.check_url(url, method)
        if rule:
            if rule.discard:
                return False
            if not rule.permits_body(body or ""):
                raise Stop("network_policy", "reviewed request fields", "request body denied")
        return True

    def discard_socket(self, url):
        parsed = urlsplit(url)
        if parsed.scheme not in {"ws", "wss"}:
            return False
        converted = parsed._replace(scheme="https" if parsed.scheme == "wss" else "http").geturl()
        try:
            rule = self.check_url(converted)
            return bool(rule and rule.discard)
        except Stop:
            return False

    def check_action(self, op: str, target: str, *, human: bool = False):
        control = self.binding.controls.get(target)
        if not control or op not in control.operations or control.risk == "blocked":
            raise Stop("action_policy", "approved control and action", "action denied")
        if control.risk == "human_only" and not human:
            raise Stop("human_required", "manual execution", "irreversible control")

    def check_live_action(self, op: str, scope: str):
        if self.binding.live_mode != "forms":
            raise Stop("action_policy", "live perception disabled", "action denied")
        grant = next((item for item in self.binding.live_scopes if item.name == scope), None)
        if grant is None or op not in grant.operations:
            raise Stop("action_policy", "reviewed live scope and action", "action denied")

    def check_fill(self, target, parameter):
        self.check_action("fill", target)
        if parameter not in self.binding.controls[target].allowed_inputs:
            raise Stop(
                "input_target_mismatch",
                "approved parameter for control",
                "binding denied",
                target=target,
            )
        receipt = self.binding.controls[target].after_fill
        if receipt and any(
            name and name != parameter
            for name in (receipt.input, self.binding.controls[receipt.target].match_input)
        ):
            raise Stop(
                "input_target_mismatch",
                "field receipt parameter",
                "receipt input mismatch",
                target=target,
            )

    def check_artifact(self, artifact):
        if (
            artifact.product != self.binding.product
            or artifact.binding_sha256 != self.binding.digest()
        ):
            raise Stop("binding_mismatch", "reviewed binding digest", "incompatible capability")
        self.check_request(artifact)
        for name in artifact.targets:
            if name in artifact.grounded and name in self.binding.controls:
                raise Stop("target_mismatch", "one target authority", "grounded/static collision")
            if name not in self.binding.controls and name not in artifact.grounded:
                raise Stop("target_mismatch")
        output_started = False
        for step in artifact.steps:
            if step.target in artifact.grounded:
                grounding = artifact.grounded[step.target]
                if step.op != grounding.operation:
                    raise Stop("target_mismatch")
                self.check_live_action(step.op, grounding.scope)
                grant = next(
                    scope for scope in self.binding.live_scopes if scope.name == grounding.scope
                )
                if step.op == "fill":
                    allowed = {
                        input_name for names in grant.fields.values() for input_name in names
                    }
                    if step.input not in allowed:
                        raise Stop(
                            "input_target_mismatch",
                            "approved parameter for live scope",
                            "binding denied",
                            target=step.target,
                        )
            else:
                self.check_action(step.op, step.target)
            if output_started and step.op != "read":
                raise Stop(
                    "output_order", "outputs collected after final action", "action after output"
                )
            output_started = step.op == "read"
            if step.target in artifact.grounded:
                parameter = None
            else:
                parameter = self.binding.controls[step.target].match_input
            if parameter and parameter not in artifact.inputs:
                raise Stop("contract_mismatch")
            if step.op == "fill" and step.target not in artifact.grounded:
                self.check_fill(step.target, step.input)
            if step.op == "read" and artifact.outputs[step.output].source != step.target:
                raise Stop("output_source_mismatch")

    def presentation_drift(self, artifact) -> tuple[str, ...]:
        """Reviewed target keys whose recorded discovery-time hint (label, role, kind
        or frame lineage) no longer matches what the current reviewed product/tenant
        presentation resolves for that same key. Informational only: replay always
        resolves targets through the current presentation, never the recorded hint.
        Callers only reach this after `check_artifact`, so every key is already
        known to exist in `self.binding.controls`.
        """
        return tuple(
            sorted(
                key
                for key, target in artifact.targets.items()
                if key in self.binding.controls
                if self.binding.controls[key].target != target
            )
        )

    def check_request(self, request):
        for output in request.outputs.values():
            control = self.binding.controls.get(output.source)
            if control is None or "read" not in control.operations:
                raise Stop("output_contract_mismatch")
            if control.match_input and control.match_input not in request.inputs:
                raise Stop("contract_mismatch")
            if output.allowed_values != control.allowed_output_values:
                raise Stop("output_contract_mismatch")
        if not request.inputs or any(
            self.binding.input_types.get(name) != spec for name, spec in request.inputs.items()
        ):
            raise Stop("contract_mismatch")
        if not request.checkpoint or any(
            condition not in request.checkpoint
            for condition in self.binding.invariants
            if condition.input is None or condition.input in request.inputs
        ):
            raise Stop("checkpoint_mismatch")
        for condition in request.checkpoint:
            if condition.target not in self.binding.controls or (
                condition.input and condition.input not in request.inputs
            ):
                raise Stop("undeclared_checkpoint")
            parameter = self.binding.controls[condition.target].match_input
            if parameter and parameter not in request.inputs:
                raise Stop("contract_mismatch")
            try:
                condition.check_input_type(request.inputs)
            except ValueError:
                raise Stop("checkpoint_type_mismatch") from None
