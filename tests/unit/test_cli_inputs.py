"""No-JSON inputs, environment-sourced credentials, and --integration resolution.

Pure argument-assembly logic, exercised directly against a plain namespace --
none of this needs a browser or a running fixture.
"""

import json
from argparse import ArgumentTypeError
from types import SimpleNamespace

import pytest

from computer_use_replay.cli import (
    MissingEnvironment,
    _apply_integration,
    _kv_pair,
    assemble_inputs,
)


def ns(**kwargs):
    base = {"inputs": "{}", "input": None, "input_env": None}
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_kv_pair_splits_on_first_equals():
    assert _kv_pair("member_id=00123") == ("member_id", "00123")
    assert _kv_pair("branch=MAIN-001 - Main Office") == ("branch", "MAIN-001 - Main Office")


def test_kv_pair_rejects_missing_equals_or_empty_key():
    with pytest.raises(ArgumentTypeError):
        _kv_pair("no-equals-sign")
    with pytest.raises(ArgumentTypeError):
        _kv_pair("=value-only")


def test_assemble_inputs_plain_json_only():
    assert assemble_inputs(ns(inputs='{"member_id":"00123"}')) == {"member_id": "00123"}


def test_assemble_inputs_merges_literal_input_pairs():
    values = assemble_inputs(ns(inputs="{}", input=[("member_id", "00123"), ("nickname", "x")]))
    assert values == {"member_id": "00123", "nickname": "x"}


def test_assemble_inputs_reads_input_env(monkeypatch):
    monkeypatch.setenv("MERIDIAN_PASSWORD", "password")
    values = assemble_inputs(ns(inputs="{}", input_env=[("password", "MERIDIAN_PASSWORD")]))
    assert values == {"password": "password"}


def test_assemble_inputs_mixes_json_literal_and_env(monkeypatch):
    monkeypatch.setenv("MERIDIAN_PASSWORD", "password")
    values = assemble_inputs(
        ns(
            inputs='{"member_id":"100234"}',
            input=[("operator", "teller1")],
            input_env=[("password", "MERIDIAN_PASSWORD")],
        )
    )
    assert values == {"member_id": "100234", "operator": "teller1", "password": "password"}


def test_assemble_inputs_rejects_invalid_json():
    with pytest.raises(ValueError, match="invalid_input_json"):
        assemble_inputs(ns(inputs="not json"))


def test_assemble_inputs_rejects_non_object_json():
    with pytest.raises(ValueError, match="invalid_input_json"):
        assemble_inputs(ns(inputs="[]"))


def test_assemble_inputs_rejects_duplicate_key_from_input(monkeypatch):
    with pytest.raises(ValueError, match="duplicate_input"):
        assemble_inputs(ns(inputs='{"member_id":"00123"}', input=[("member_id", "00456")]))


def test_assemble_inputs_rejects_duplicate_key_from_input_env(monkeypatch):
    monkeypatch.setenv("X", "y")
    with pytest.raises(ValueError, match="duplicate_input"):
        assemble_inputs(ns(inputs='{"member_id":"00123"}', input_env=[("member_id", "X")]))


def test_assemble_inputs_missing_env_var_names_the_variable(monkeypatch):
    monkeypatch.delenv("MERIDIAN_PASSWORD", raising=False)
    with pytest.raises(MissingEnvironment) as excinfo:
        assemble_inputs(ns(input_env=[("password", "MERIDIAN_PASSWORD")]))
    assert excinfo.value.variable == "MERIDIAN_PASSWORD"


def test_missing_environment_message_never_carries_a_value():
    exc = MissingEnvironment("MERIDIAN_PASSWORD")
    assert "MERIDIAN_PASSWORD" in str(exc)


# --- --integration ---


def integration_args(**kwargs):
    base = {
        "integration": None,
        "target": "http://127.0.0.1:8765",
        "binding": "profiles/juniper.json",
        "capabilities": "capabilities",
        "request": "requests/read_savings.json",
        "artifact": "runs/read_savings.json",
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_apply_integration_noop_when_not_given():
    args = integration_args()
    _apply_integration(args)
    assert args.target == "http://127.0.0.1:8765"


def test_apply_integration_fills_defaults_for_discover_replay(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "integrations" / "demo").mkdir(parents=True)
    (tmp_path / "integrations" / "demo" / "integration.json").write_text(
        json.dumps(
            {
                "target": "https://example.invalid",
                "binding": "integrations/demo/profile.json",
                "request": "integrations/demo/request.json",
                "capabilities": "integrations/demo",
                "capability": "read_savings",
            }
        )
    )
    args = integration_args(integration="demo")
    _apply_integration(args)
    assert args.target == "https://example.invalid"
    assert args.binding == "integrations/demo/profile.json"
    assert args.request == "integrations/demo/request.json"
    assert args.artifact == "integrations/demo/read_savings.json"


def test_apply_integration_never_overrides_an_explicit_flag(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "integrations" / "demo").mkdir(parents=True)
    (tmp_path / "integrations" / "demo" / "integration.json").write_text(
        json.dumps({"target": "https://example.invalid", "binding": "b.json"})
    )
    args = integration_args(integration="demo", target="https://explicit.invalid")
    _apply_integration(args)
    assert args.target == "https://explicit.invalid"
    assert args.binding == "b.json"


def test_apply_integration_fills_request_when_unset_none(tmp_path, monkeypatch):
    """discover's --request has no factory-default string, unlike every other
    field -- its argparse default is None (goal-only, unset). With no --goal
    given either, that None is still "not given", exactly like the factory
    default string is for target/binding/capabilities.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "integrations" / "demo").mkdir(parents=True)
    (tmp_path / "integrations" / "demo" / "integration.json").write_text(
        json.dumps({"request": "integrations/demo/request.json"})
    )
    args = integration_args(integration="demo", request=None)
    _apply_integration(args)
    assert args.request == "integrations/demo/request.json"


def test_apply_integration_leaves_request_none_for_an_explicit_goal(tmp_path, monkeypatch):
    """An explicit --goal with no --request is a deliberate goal-only choice --
    an integration's fixed request.json must not silently turn that into
    file-based discovery. Other fields still fill in normally alongside it.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "integrations" / "demo").mkdir(parents=True)
    (tmp_path / "integrations" / "demo" / "integration.json").write_text(
        json.dumps(
            {"target": "https://example.invalid", "request": "integrations/demo/request.json"}
        )
    )
    args = integration_args(integration="demo", request=None, goal="Look something up.")
    _apply_integration(args)
    assert args.request is None
    assert args.target == "https://example.invalid"


def test_apply_integration_leaves_artifact_default_without_a_named_capability(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "integrations" / "demo").mkdir(parents=True)
    (tmp_path / "integrations" / "demo" / "integration.json").write_text(
        json.dumps({"capabilities": "integrations/demo"})
    )
    args = integration_args(integration="demo")
    _apply_integration(args)
    assert args.artifact == "runs/read_savings.json"


def test_apply_integration_catalog_has_no_artifact_or_request_attr(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "integrations" / "demo").mkdir(parents=True)
    (tmp_path / "integrations" / "demo" / "integration.json").write_text(
        json.dumps(
            {"binding": "integrations/demo/profile.json", "capabilities": "integrations/demo"}
        )
    )
    args = SimpleNamespace(
        integration="demo", binding="profiles/juniper.json", capabilities="capabilities"
    )
    _apply_integration(args)
    assert args.binding == "integrations/demo/profile.json"
    assert args.capabilities == "integrations/demo"


def test_apply_integration_unknown_name_raises_value_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = integration_args(integration="does-not-exist")
    with pytest.raises(ValueError):
        _apply_integration(args)


def test_apply_integration_rejects_malformed_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "integrations" / "demo").mkdir(parents=True)
    (tmp_path / "integrations" / "demo" / "integration.json").write_text("not json")
    args = integration_args(integration="demo")
    with pytest.raises(ValueError):
        _apply_integration(args)


def test_apply_integration_rejects_non_object_descriptor(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "integrations" / "demo").mkdir(parents=True)
    (tmp_path / "integrations" / "demo" / "integration.json").write_text("[1, 2, 3]")
    args = integration_args(integration="demo")
    with pytest.raises(ValueError):
        _apply_integration(args)
