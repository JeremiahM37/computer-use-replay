"""Meridian Core integration: reviewed profile/request/artifact load and stay
policy-compatible offline, mirroring how the base juniper profile is checked
(tests/unit/test_contracts.py, tests/unit/test_policy.py). No network access:
only Binding/GoalRequest/Capability/Policy validation against the checked-in
JSON files."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from computer_use_replay.contracts import Capability, GoalRequest
from computer_use_replay.policy import Binding, Policy, Stop

ORIGIN = "https://web-sample.interface-hiring.com"


@pytest.fixture
def binding():
    return Binding.load(Path("integrations/meridian/profile.json"))


@pytest.fixture
def request_task():
    return GoalRequest.load(Path("integrations/meridian/request.json"))


@pytest.fixture
def artifact():
    return Capability.model_validate_json(
        Path("integrations/meridian/read_savings.json").read_text()
    )


def test_profile_and_request_load_and_are_policy_compatible(binding, request_task):
    assert binding.product == "meridian_core"
    assert binding.entry == "/signon"
    policy = Policy(binding, ORIGIN)
    policy.check_request(request_task)


def test_committed_artifact_matches_the_reviewed_binding(binding, artifact):
    assert artifact.binding_sha256 == binding.digest()
    assert artifact.provenance.mode == "llm"
    assert artifact.provenance.calls > 0
    Policy(binding, ORIGIN).check_artifact(artifact)


def test_artifact_never_reads_before_the_final_action(artifact):
    # Output collection is terminal: no fill/click may follow a read.
    ops = [step.op for step in artifact.steps]
    reads = [i for i, op in enumerate(ops) if op == "read"]
    assert reads and reads == list(range(min(reads), len(ops)))


@pytest.mark.parametrize(
    "url,method",
    [
        (ORIGIN + "/signon", "GET"),
        (ORIGIN + "/menu", "GET"),
        (ORIGIN + "/members", "GET"),
        (ORIGIN + "/members?by=number&q=100234", "GET"),
        (ORIGIN + "/members/100234", "GET"),
        (ORIGIN + "/signon", "POST"),
    ],
)
def test_the_read_only_flow_routes_are_allowed(binding, url, method):
    Policy(binding, ORIGIN).check_network_request(url, method)


@pytest.mark.parametrize(
    "url,method",
    [
        (ORIGIN + "/", "GET"),  # unauthenticated redirect target; not an entry or grant
        (ORIGIN + "/members?by=name&q=Turing", "GET"),  # search-by-number only is reviewed
        (ORIGIN + "/members/100234?inject=timeout", "GET"),  # test-only fault injection
        (ORIGIN + "/settings", "GET"),  # fault-injection admin panel; never allowlisted
        (ORIGIN + "/members/100234/transfer", "GET"),  # state-changing surface; not onboarded
        (ORIGIN + "/members/100234", "POST"),  # read-only route; POST never granted
        ("https://evil.example.com/members/100234", "GET"),  # different origin entirely
    ],
)
def test_anything_outside_the_read_only_flow_is_denied(binding, url, method):
    with pytest.raises(Stop):
        Policy(binding, ORIGIN).check_network_request(url, method)


def test_login_redirect_is_reviewed_for_exactly_one_destination(binding):
    rules = [r for r in binding.request_rules if r.path == "/signon" and "POST" in r.methods]
    assert len(rules) == 1
    assert rules[0].redirect_to == ("/menu",)


def test_declared_business_and_human_states_match_the_real_app(binding):
    kinds = {name: state.kind for name, state in binding.states.items()}
    assert kinds == {
        "member_not_found": "business",
        "validation_error": "business",
        "permission_denied": "business",
        "session_expired": "human",
    }
    expired = binding.states["session_expired"]
    recovery = binding.controls[expired.manual_recovery]
    assert recovery.risk == "human_only" and "click" in recovery.operations


def test_state_changing_controls_are_kept_out_of_the_reviewed_vocabulary(binding):
    # This profile only onboards the read-only lookup: nothing named after a
    # money-moving or record-changing action belongs in its controls at all.
    forbidden = {"transfer", "hold", "update", "open_share", "create", "finalize"}
    assert not (forbidden & set(binding.controls))


def test_credentials_are_typed_inputs_never_embedded_in_the_profile(binding):
    # Credentials are supplied at invocation as typed text inputs, never a
    # literal demo operator id/password baked into the reviewed vocabulary.
    assert binding.input_types["operator"].kind == "text"
    assert binding.input_types["operator"].pattern is None
    assert binding.input_types["password"].kind == "text"
    assert binding.input_types["password"].pattern is None
    dumped = json.dumps(binding.model_dump(mode="json"))
    assert "teller1" not in dumped
    assert "super1" not in dumped


def test_member_id_pattern_matches_every_sample_member(binding):
    spec = binding.input_types["member_id"]
    for member in ["100234", "100987", "101555", "102777", "103001"]:
        spec.validate_value(member)
    with pytest.raises(ValueError):
        spec.validate_value("12345")  # five digits: not this app's member number shape


def test_request_rejects_a_binding_from_a_different_product():
    other = Binding.load(Path("profiles/juniper.json"))
    request_task = GoalRequest.load(Path("integrations/meridian/request.json"))
    with pytest.raises(Stop):
        Policy(other, ORIGIN).check_request(request_task)


def test_malformed_json_files_fail_loudly(tmp_path):
    bad = tmp_path / "broken.json"
    bad.write_text("{not json")
    with pytest.raises(ValidationError):
        Binding.load(bad)
    with pytest.raises(ValidationError):
        GoalRequest.load(bad)
