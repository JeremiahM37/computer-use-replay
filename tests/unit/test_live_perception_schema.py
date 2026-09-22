"""Schema and authority-boundary checks for dynamic live controls."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from computer_use_replay.contracts import (
    Capability,
    Fill,
    GoalRequest,
    Grounding,
    LiveScope,
    Read,
    Target,
)
from computer_use_replay.policy import Binding, Policy, Stop

LIVE = Path("profiles/juniper_live.json")


def raw_live():
    return json.loads(LIVE.read_text())


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"live_mode": "forms", "live_scopes": []}, "live mode requires reviewed scopes"),
        (
            {"live_mode": "off", "live_scopes": raw_live()["live_scopes"]},
            "live scopes require live mode",
        ),
        (
            {
                "live_mode": "forms",
                "live_scopes": [
                    {
                        **raw_live()["live_scopes"][0],
                        "fields": {"member_key": []},
                    }
                ],
            },
            "live field requires input mapping",
        ),
    ],
)
def test_live_binding_rejects_unreviewed_or_empty_grants(changes, message):
    raw = raw_live()
    raw.update(changes)
    with pytest.raises(ValidationError, match=message):
        Binding.model_validate(raw)


def test_live_scope_requires_explicit_action_and_method():
    with pytest.raises(ValidationError):
        LiveScope(name="lookup", container="form", action="", methods=("POST",))


def test_grounding_rejects_unknown_robustness():
    with pytest.raises(ValidationError):
        Grounding(
            scope="lookup",
            operation="fill",
            target={"kind": "css", "name": "form [name=x]"},
            role="field",
            robustness="fuzzy",
        )


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"frame": ("other",)}, "frame mismatch"),
        ({"robustness": "role"}, "role target"),
        ({"robustness": "label"}, "label target"),
        (
            {
                "robustness": "scope_css",
                "target": {"kind": "label", "name": "Member", "frames": ["workbench"]},
            },
            "CSS target",
        ),
    ],
)
def test_grounding_rejects_inconsistent_provenance(changes, message):
    raw = {
        "scope": "lookup",
        "operation": "fill",
        "target": {"kind": "css", "name": "form [name=x]", "frames": ["workbench"]},
        "role": "field",
        "frame": ("workbench",),
        "robustness": "scope_css",
    }
    raw.update(changes)
    with pytest.raises(ValidationError, match=message):
        Grounding(**raw)


def test_legacy_binding_stays_live_mode_off_and_has_no_scopes():
    binding = Binding.load(Path("profiles/juniper.json"))
    assert binding.live_mode == "off"
    assert binding.live_scopes == ()


def test_grounded_capability_requires_and_serializes_schema3(capability):
    target = Target(kind="css", name='form [name="member_key"]', frames=("workbench",))
    grounding = Grounding(
        scope="lookup",
        operation="fill",
        target=target,
        role="field",
        frame=("workbench",),
        label_source="structural",
        robustness="scope_css",
    )
    dynamic = capability.model_copy(
        update={
            "targets": {**capability.targets, "live_field": target},
            "steps": (Fill(target="live_field", input="member_id"),),
            "grounded": {"live_field": grounding},
        }
    )
    with pytest.raises(ValidationError, match="schema 3.0"):
        dynamic.model_validate(dynamic.model_dump())
    dynamic = dynamic.model_copy(update={"schema_version": "3.0"})
    wire = dynamic.model_dump()
    assert wire["schema_version"] == "3.0"
    assert wire["grounded"]["live_field"]["scope"] == "lookup"


def test_grounded_capability_rejects_forged_identity(capability):
    target = Target(kind="css", name='form [name="member_key"]', frames=("workbench",))
    grounding = Grounding(
        scope="lookup",
        operation="fill",
        target=target,
        role="field",
        frame=("workbench",),
        robustness="scope_css",
    )
    raw = capability.model_dump()
    raw.update(
        schema_version="3.0",
        targets={**raw["targets"], "live_field": {**target.model_dump(), "name": "other"}},
        steps=[{"op": "fill", "target": "live_field", "input": "member_id"}],
        grounded={"live_field": grounding.model_dump()},
    )
    with pytest.raises(ValidationError, match="grounding target mismatch"):
        Capability.model_validate(raw)


def test_grounded_capability_rejects_forged_key_and_step(capability):
    target = Target(kind="css", name='form [name="member_key"]', frames=("workbench",))
    grounding = Grounding(
        scope="lookup",
        operation="fill",
        target=target,
        role="field",
        frame=("workbench",),
        robustness="scope_css",
    )
    raw = capability.model_dump()
    raw.update(
        schema_version="3.0",
        targets={**raw["targets"], "live_field": target.model_dump()},
        steps=[{"op": "fill", "target": "live_field", "input": "member_id"}],
        grounded={"forged": grounding.model_dump()},
    )
    with pytest.raises(ValidationError, match="dynamic targets require live prefix"):
        Capability.model_validate(raw)
    raw["grounded"] = {"live_field": grounding.model_dump()}
    raw["steps"] = [{"op": "read", "target": "live_field", "output": "available_balance"}]
    with pytest.raises(ValidationError, match="live targets cannot read outputs"):
        Capability.model_validate(raw)
    raw["steps"] = [{"op": "fill", "target": "live_field", "input": "member_id"}]
    raw["grounded"]["live_field"]["operation"] = "click"
    with pytest.raises(ValidationError, match="grounding operation mismatch"):
        Capability.model_validate(raw)


def test_grounded_capability_rejects_missing_target_entry(capability):
    target = Target(kind="css", name='form [name="member_key"]', frames=("workbench",))
    grounding = Grounding(
        scope="lookup",
        operation="fill",
        target=target,
        role="field",
        frame=("workbench",),
        robustness="scope_css",
    )
    raw = capability.model_dump()
    raw.update(
        schema_version="3.0",
        steps=[{"op": "fill", "target": "live_field", "input": "member_id"}],
        grounded={"live_field": grounding.model_dump()},
    )
    with pytest.raises(ValidationError, match="grounding target missing"):
        Capability.model_validate(raw)


@pytest.mark.parametrize("field", ["methods", "operations"])
def test_live_scope_rejects_empty_action_permissions(field):
    with pytest.raises(ValidationError):
        LiveScope(
            name="lookup",
            container="form",
            action="/desk/search",
            **{field: ()},
        )


def test_live_scope_rejects_unbounded_field_name():
    with pytest.raises(ValidationError, match="invalid live field"):
        LiveScope(
            name="lookup",
            container="form",
            action="/desk/search",
            fields={"x" * 101: ("member_id",)},
        )


def test_binding_rejects_live_scope_action_outside_route_grants():
    raw = raw_live()
    raw["live_scopes"][0]["action"] = "https://evil.example/submit"
    with pytest.raises(ValidationError, match="allowed relative route"):
        Binding.model_validate(raw)


def test_binding_rejects_live_post_without_body_grant():
    raw = raw_live()
    raw["request_rules"] = [rule for rule in raw["request_rules"] if rule["path"] != "/desk/search"]
    with pytest.raises(ValidationError, match="non-discard body grant"):
        Binding.model_validate(raw)


def test_binding_rejects_live_field_missing_from_body_grant():
    raw = raw_live()
    raw["request_rules"][0]["body_keys"] = []
    with pytest.raises(ValidationError, match="body grant keys"):
        Binding.model_validate(raw)


def test_binding_allows_get_only_live_scope():
    raw = raw_live()
    raw["live_scopes"][0]["methods"] = ["GET"]
    binding = Binding.model_validate(raw)
    assert binding.live_scopes[0].methods == ("GET",)


def test_binding_rejects_ambiguous_post_body_grants():
    raw = raw_live()
    raw["request_rules"].append(dict(raw["request_rules"][0]))
    with pytest.raises(ValidationError, match="non-discard body grant"):
        Binding.model_validate(raw)


def test_binding_rejects_live_scope_unknown_symbolic_input():
    raw = raw_live()
    raw["live_scopes"][0]["fields"]["member_key"] = ["unreviewed_input"]
    with pytest.raises(ValidationError, match="undefined live input"):
        Binding.model_validate(raw)


def test_binding_rejects_duplicate_live_scope():
    raw = raw_live()
    raw["live_scopes"].append(dict(raw["live_scopes"][0]))
    with pytest.raises(ValidationError, match="duplicate live scope"):
        Binding.model_validate(raw)


def test_policy_rejects_grounded_fill_outside_scope_inputs():
    binding = Binding.load(LIVE)
    request = GoalRequest.load(Path("requests/read_savings.json"))
    policy = Policy(binding, "http://127.0.0.1:9999")
    target = Target(kind="css", name='form [name="member_key"]', frames=("workbench",))
    grounding = Grounding(
        scope="lookup",
        operation="fill",
        target=target,
        role="field",
        frame=("workbench",),
        robustness="scope_css",
    )
    artifact = Capability(
        schema_version="3.0",
        name="grounded_lookup",
        product=binding.product,
        binding_sha256=binding.digest(),
        targets={k: v.target for k, v in binding.controls.items()} | {"live_field": target},
        inputs=dict(binding.input_types),
        outputs=request.outputs,
        steps=(
            Fill(target="live_field", input="nickname"),
            Read(target="balance", output="available_balance"),
        ),
        checkpoint=binding.invariants,
        grounded={"live_field": grounding},
        provenance={"mode": "test_fixture", "model": "fixture", "calls": 0, "run_id": "unit"},
    )
    with pytest.raises(Stop) as caught:
        policy.check_artifact(artifact)
    assert caught.value.code == "input_target_mismatch"

    collision_binding = binding.model_copy(
        update={"controls": {**binding.controls, "live_field": binding.controls["balance"]}}
    )
    collision = artifact.model_copy(update={"binding_sha256": collision_binding.digest()})
    with pytest.raises(Stop) as caught:
        Policy(collision_binding, "http://127.0.0.1:9999").check_artifact(collision)
    assert caught.value.code == "target_mismatch"

    artifact.grounded["live_field"] = grounding.model_copy(update={"operation": "click"})
    with pytest.raises(Stop) as caught:
        policy.check_artifact(artifact)
    assert caught.value.code == "target_mismatch"


def test_policy_rejects_live_action_without_grant():
    legacy = Binding.load(Path("profiles/juniper.json"))
    with pytest.raises(Stop) as caught:
        Policy(legacy, "http://127.0.0.1:9999").check_live_action("click", "lookup")
    assert caught.value.code == "action_policy"
    live = Binding.load(LIVE)
    with pytest.raises(Stop) as caught:
        Policy(live, "http://127.0.0.1:9999").check_live_action("read", "lookup")
    assert caught.value.code == "action_policy"


def test_policy_rejects_static_fill_parameter_mismatch():
    binding = Binding.load(Path("profiles/juniper.json"))
    with pytest.raises(Stop) as caught:
        Policy(binding, "http://127.0.0.1:9999").check_fill("member_input", "nickname")
    assert caught.value.code == "input_target_mismatch"
