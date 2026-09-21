"""Agent-facing capability catalog: typed schemas, no invented prose or example values."""

import json
from pathlib import Path

import pytest

from computer_use_replay.catalog import (
    _input_schema,
    _output_schema,
    build_catalog,
    capability_entry,
    resolve_capability,
    unavailable_capabilities,
)
from computer_use_replay.contracts import Capability, Input, Output
from computer_use_replay.policy import Stop


def test_catalog_lists_both_shipped_capabilities_with_typed_schemas(binding):
    entries = build_catalog(Path("capabilities"), binding)
    assert {entry["name"] for entry in entries} == {"read_savings", "prepare_subaccount"}
    for entry in entries:
        artifact = Capability.model_validate_json(
            (Path("capabilities") / f"{entry['name']}.json").read_text()
        )
        assert entry["artifact_sha256"] == artifact.digest()
        assert set(entry["parameters"]["properties"]) == set(artifact.inputs)
        assert entry["parameters"]["required"] == sorted(artifact.inputs)
        assert entry["parameters"]["additionalProperties"] is False
        assert set(entry["output"]["properties"]) == set(artifact.outputs)
        assert all(
            spec["x-sensitive"] is True for spec in entry["parameters"]["properties"].values()
        )
        assert all(spec["x-sensitive"] is True for spec in entry["output"]["properties"].values())
        assert entry["result_statuses"]["business_outcome"]["codes"] == sorted(
            key for key, state in binding.states.items() if state.kind == "business"
        )
        assert entry["result_statuses"]["success"]["description"]
        assert entry["result_statuses"]["failure"]["description"]


def test_catalog_never_invents_prose_or_example_values(binding):
    text = json.dumps(build_catalog(Path("capabilities"), binding))
    assert "example" not in text.lower()
    # No realistic sample identifier, nickname or goal text anywhere in the output --
    # the description is built only from typed names, never the discovery goal.
    assert "00123" not in text and "00456" not in text and "Rainy day" not in text
    assert "member" in text  # still descriptive, just from the declared input name


def test_capability_entry_rejects_product_mismatch(binding, capability):
    other = binding.model_copy(update={"product": "other_product"})
    with pytest.raises(Stop, match="catalog_binding_mismatch"):
        capability_entry(capability, other)


def test_resolve_capability_by_name_ambiguity_and_absence(tmp_path, capability):
    (tmp_path / "a.json").write_text(capability.model_dump_json())
    assert resolve_capability(tmp_path, "read_savings") == tmp_path / "a.json"
    with pytest.raises(Stop, match="capability_not_found"):
        resolve_capability(tmp_path, "does_not_exist")
    (tmp_path / "b.json").write_text(capability.model_dump_json())
    with pytest.raises(Stop, match="capability_name_ambiguous"):
        resolve_capability(tmp_path, "read_savings")


@pytest.mark.parametrize(
    "spec,expected",
    [
        (Input(kind="identifier", pattern=r"^\d{5}$"), {"type": "string", "pattern": r"^\d{5}$"}),
        (Input(kind="text", max_length=40), {"type": "string", "maxLength": 40}),
        (Input(kind="multiline", max_length=200), {"type": "string", "maxLength": 200}),
        (
            Input(kind="integer", minimum=0, maximum=10),
            {"type": "integer", "minimum": 0, "maximum": 10},
        ),
        (Input(kind="integer"), {"type": "integer"}),
        (Input(kind="boolean"), {"type": "boolean"}),
    ],
)
def test_input_schema_reflects_each_typed_kind(spec, expected):
    schema = _input_schema(spec)
    assert schema["x-sensitive"] is True
    for key, value in expected.items():
        assert schema[key] == value
    if spec.kind != "integer":
        assert "minimum" not in schema and "maximum" not in schema


@pytest.mark.parametrize(
    "spec,expected",
    [
        (Output(kind="money"), {"type": "object"}),
        (Output(kind="text"), {"type": "string"}),
        (Output(kind="text", allowed_values=("A", "B")), {"type": "string", "enum": ["A", "B"]}),
    ],
)
def test_output_schema_reflects_kind_and_enum(spec, expected):
    schema = _output_schema(spec)
    assert schema["x-sensitive"] is True
    for key, value in expected.items():
        assert schema[key] == value
    if not spec.allowed_values:
        assert "enum" not in schema


def test_catalog_ignores_profiles_requests_and_other_json_beside_an_artifact(
    tmp_path, binding, capability
):
    (tmp_path / "read_savings.json").write_text(capability.model_dump_json())
    (tmp_path / "profile.json").write_text(Path("profiles/juniper.json").read_text())
    (tmp_path / "request.json").write_text(Path("requests/read_savings.json").read_text())
    (tmp_path / "list.json").write_text("[1, 2, 3]")
    assert [entry["name"] for entry in build_catalog(tmp_path, binding)] == ["read_savings"]
    assert resolve_capability(tmp_path, "read_savings") == tmp_path / "read_savings.json"


def test_catalog_still_rejects_a_damaged_artifact_instead_of_hiding_it(
    tmp_path, binding, capability
):
    damaged = json.loads(capability.model_dump_json())
    damaged["steps"] = []
    (tmp_path / "damaged.json").write_text(json.dumps(damaged))
    with pytest.raises(ValueError):
        build_catalog(tmp_path, binding)


def test_catalog_describes_an_integration_directory_with_sensitive_credentials():
    from computer_use_replay.policy import Binding

    directory = Path("integrations/meridian")
    entries = build_catalog(directory, Binding.load(directory / "profile.json"))
    assert [entry["name"] for entry in entries] == ["read_savings"]
    properties = entries[0]["parameters"]["properties"]
    assert properties and all(spec.get("x-sensitive") is True for spec in properties.values())


def test_catalog_describes_the_erpnext_integration_directory_with_sensitive_credentials():
    from computer_use_replay.policy import Binding

    directory = Path("integrations/erpnext")
    entries = build_catalog(directory, Binding.load(directory / "profile.json"))
    assert [entry["name"] for entry in entries] == ["prepare_quotation"]
    properties = entries[0]["parameters"]["properties"]
    assert set(properties) == {
        "username",
        "password",
        "customer_id",
        "item_id",
        "quantity",
        "order_type",
    }
    assert all(spec.get("x-sensitive") is True for spec in properties.values())


def test_catalog_never_advertises_an_artifact_the_policy_would_refuse(
    tmp_path, binding, capability
):
    (tmp_path / "current.json").write_text(capability.model_dump_json())
    stale = json.loads(capability.model_dump_json())
    stale["name"] = "stale_lookup"
    stale["binding_sha256"] = "0" * 64
    (tmp_path / "stale.json").write_text(json.dumps(stale))
    assert [entry["name"] for entry in build_catalog(tmp_path, binding)] == ["read_savings"]
    assert unavailable_capabilities(tmp_path, binding) == [
        {"file": "stale.json", "name": "stale_lookup", "reason": "binding_mismatch"}
    ]
    assert unavailable_capabilities(Path("capabilities"), binding) == []
