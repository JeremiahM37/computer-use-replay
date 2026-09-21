"""Agent-facing capability catalog: typed tool/function definitions for saved artifacts.

Reuses the same Capability/Binding contracts as discovery and replay. Invoking a
catalog entry by name runs the identical replay path in cli.py -- this module never
forks the engine, it only describes what is already saved and locates it by name.
"""

from __future__ import annotations

import json
from pathlib import Path

from computer_use_replay.contracts import Capability, Input, Output
from computer_use_replay.policy import Binding, Policy, Stop

_INTEGER_BOUNDS = ("minimum", "maximum")


def _input_schema(spec: Input) -> dict:
    # Every Input is sensitive by contract (contracts.Input.sensitive is always
    # True): flagged here, and never given a realistic example value.
    schema: dict = {"x-sensitive": True}
    if spec.kind == "identifier":
        schema.update(type="string", pattern=spec.pattern, maxLength=spec.max_length)
    elif spec.kind in {"text", "multiline"}:
        schema.update(type="string", maxLength=spec.max_length)
    elif spec.kind == "integer":
        schema["type"] = "integer"
        for bound in _INTEGER_BOUNDS:
            value = getattr(spec, bound)
            if value is not None:
                schema[bound] = value
    else:
        schema["type"] = "boolean"
    return schema


def _output_schema(spec: Output) -> dict:
    # Every Output is sensitive by contract too; caller stdout may return the
    # value, but nothing here invents or echoes one.
    if spec.kind == "money":
        return {
            "x-sensitive": True,
            "type": "object",
            "properties": {"amount": {"type": "string"}, "currency": {"const": "USD"}},
            "required": ["amount", "currency"],
        }
    schema = {"x-sensitive": True, "type": "string"}
    if spec.allowed_values:
        schema["enum"] = list(spec.allowed_values)
    return schema


def _description(capability: Capability) -> str:
    # Built only from typed names already on the artifact -- never invented prose,
    # and never the discovery goal text, which is not persisted onto a Capability.
    inputs = ", ".join(sorted(capability.inputs)) or "none"
    outputs = ", ".join(sorted(capability.outputs)) or "none"
    return (
        f"Reviewed capability '{capability.name}' for product '{capability.product}'. "
        f"Typed inputs: {inputs}. Typed outputs: {outputs}."
    )


def capability_entry(capability: Capability, binding: Binding) -> dict:
    """One agent-facing tool/function definition for a saved capability, resolved
    against the reviewed product binding it declares (for the product's declared
    business outcome codes).
    """
    if capability.product != binding.product:
        raise Stop("catalog_binding_mismatch")
    outcome_codes = sorted(key for key, state in binding.states.items() if state.kind == "business")
    return {
        "name": capability.name,
        "description": _description(capability),
        "parameters": {
            "type": "object",
            "properties": {name: _input_schema(spec) for name, spec in capability.inputs.items()},
            "required": sorted(capability.inputs),
            "additionalProperties": False,
        },
        "output": {
            "type": "object",
            "properties": {name: _output_schema(spec) for name, spec in capability.outputs.items()},
            "additionalProperties": False,
        },
        "result_statuses": {
            "success": {"description": "typed outputs returned"},
            "business_outcome": {"codes": outcome_codes},
            "failure": {"description": "technical failure; see the result's failure code"},
        },
        "artifact_sha256": capability.digest(),
    }


def _saved_capabilities(directory: Path):
    """Saved artifacts in a directory that may also hold profiles, requests or manifests.

    A JSON object is treated as a capability only if it declares both `schema_version`
    and `steps`; it must then validate strictly, so a damaged artifact still fails loudly
    instead of silently disappearing from the catalog.
    """
    for path in sorted(directory.glob("*.json")):
        data = json.loads(path.read_text())
        if isinstance(data, dict) and "schema_version" in data and "steps" in data:
            yield path, Capability.model_validate(data)


def _classify(directory: Path, binding: Binding):
    """Split saved artifacts into those the current reviewed policy would run and those it
    would refuse, using the very check `replay`/`invoke` apply -- so the catalog can never
    advertise a capability that invocation then rejects.
    """
    policy = Policy(binding, "https://catalog.invalid")
    ready, unavailable = [], []
    for path, artifact in _saved_capabilities(directory):
        try:
            policy.check_artifact(artifact)
        except Stop as stop:
            unavailable.append({"file": path.name, "name": artifact.name, "reason": stop.code})
        else:
            ready.append(artifact)
    return ready, unavailable


def build_catalog(directory: Path, binding: Binding) -> list[dict]:
    return [capability_entry(artifact, binding) for artifact in _classify(directory, binding)[0]]


def unavailable_capabilities(directory: Path, binding: Binding) -> list[dict]:
    """Saved artifacts the current policy refuses (stale fingerprint, other product, ...)."""
    return _classify(directory, binding)[1]


def resolve_capability(directory: Path, name: str) -> Path:
    """The single saved artifact path whose capability name matches `name`."""
    matches = [path for path, artifact in _saved_capabilities(directory) if artifact.name == name]
    if not matches:
        raise Stop("capability_not_found")
    if len(matches) > 1:
        raise Stop("capability_name_ambiguous")
    return matches[0]
