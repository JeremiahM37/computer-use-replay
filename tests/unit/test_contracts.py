"""Strict capability schemas and explicitly typed field behavior."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from computer_use_replay.contracts import (
    Capability,
    Click,
    Condition,
    Fill,
    GoalRequest,
    Input,
    Output,
    Provenance,
    Read,
    Review,
    Success,
    Target,
)
from computer_use_replay.policy import Binding, Control, Policy, Stop

TASK = GoalRequest.load(Path("requests/read_savings.json"))


@pytest.fixture
def binding():
    return Binding.load(Path("profiles/juniper.json"))


@pytest.fixture
def artifact(binding):
    return Capability(
        name="read_savings",
        product=binding.product,
        binding_sha256=binding.digest(),
        targets={k: v.target for k, v in binding.controls.items()},
        inputs=TASK.inputs,
        outputs=TASK.outputs,
        steps=(
            Fill(target="member_input", input="member_id"),
            Click(target="search", after=Condition(target="results_screen")),
            Read(target="balance", output="available_balance"),
        ),
        checkpoint=TASK.checkpoint,
        provenance=Provenance(mode="test_fixture", model="fixture", calls=0, run_id="unit"),
    )


def test_roundtrip(artifact):
    assert Capability.model_validate_json(artifact.model_dump_json()) == artifact
    assert len(artifact.digest()) == 64


@pytest.mark.parametrize("value", ["00123", "00456", "00000", "99999"])
def test_identifiers_keep_leading_zeroes(value):
    assert Input(kind="identifier", pattern=r"[0-9]{5}").validate_value(value) == value


@pytest.mark.parametrize("value", [123, True, None, "", "123", "１２３４５", "00123\n", "00123;"])
def test_identifier_rejects_coercion(value):
    with pytest.raises(ValueError):
        Input(kind="identifier", pattern=r"[0-9]{5}").validate_value(value)


@pytest.mark.parametrize(
    "kind,value", [("boolean", "false"), ("integer", True), ("text", ""), ("text", 123)]
)
def test_strict_input_types(kind, value):
    with pytest.raises(ValueError):
        Input(kind=kind).validate_value(value)


@pytest.mark.parametrize(
    "raw,amount", [("$1,204.57", "1204.57"), ("$0.00", "0.00"), ("$1000.10", "1000.10")]
)
def test_money_decimal(raw, amount):
    assert Output(kind="money").parse(raw) == {"amount": amount, "currency": "USD"}


@pytest.mark.parametrize("raw", ["$NaN", "$1,20.00", "$01.00", "$2.001", "123.00", "$-1.00", ""])
def test_invalid_money(raw):
    with pytest.raises(ValueError):
        Output(kind="money").parse(raw)


@pytest.mark.parametrize(
    "mutation", ["version", "extra", "input", "output", "target", "checkpoint", "duplicate"]
)
def test_invalid_artifact_rejected(artifact, mutation):
    raw = artifact.model_dump(mode="json")
    if mutation == "version":
        raw["schema_version"] = "99.0"
    if mutation == "extra":
        raw["execute_python"] = "print(1)"
    if mutation == "input":
        raw["steps"][0]["input"] = "not_declared"
    if mutation == "output":
        raw["steps"][-1]["output"] = "not_declared"
    if mutation == "target":
        raw["steps"][0]["target"] = "not_declared"
    if mutation == "checkpoint":
        raw["checkpoint"] = []
    if mutation == "duplicate":
        raw["steps"].append(raw["steps"][-1])
    with pytest.raises(ValidationError):
        Capability.model_validate(raw)


def test_args_exact(artifact):
    assert artifact.arguments({"member_id": "00123"}) == {"member_id": "00123"}
    for args in [{}, {"member_id": "00123", "extra": 1}, {"member_id": 123}]:
        with pytest.raises(ValueError):
            artifact.arguments(args)


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost:9000/",
        "http://localhost:9001/",
        "http://evil.test/",
        "http://localhost:9000/?token=x",
        "http://localhost:9000/desk/%73earch",
        "http://localhost:9000/desk/../search",
        "file:///etc/passwd",
        "http://user@localhost:9000/",
        "http://localhost:9000/desk/finalize",
    ],
)
def test_network_deny_by_default(binding, url):
    with pytest.raises(Stop):
        Policy(binding, "http://localhost:9000").check_url(url)


def test_method_and_action_allowlists(binding):
    policy = Policy(binding, "http://localhost:9000")
    policy.check_url("http://localhost:9000/desk/search", "POST")
    with pytest.raises(Stop):
        policy.check_url("http://localhost:9000/desk/search", "DELETE")
    with pytest.raises(Stop):
        policy.check_action("click", "balance")
    with pytest.raises(Stop, match="human_required"):
        policy.check_action("click", "finalize")


def test_artifact_cannot_downgrade_policy(artifact, binding):
    policy = Policy(binding, "http://localhost:9000")
    policy.check_artifact(artifact)
    raw = artifact.model_dump(mode="json")
    raw["steps"][1]["target"] = "finalize"
    with pytest.raises(Stop, match="human_required"):
        policy.check_artifact(Capability.model_validate(raw))
    raw = artifact.model_dump(mode="json")
    raw["targets"]["unknown_control"] = raw["targets"].pop("search")
    raw["steps"][1]["target"] = "unknown_control"
    with pytest.raises(Stop, match="target_mismatch"):
        policy.check_artifact(Capability.model_validate(raw))


def test_checkpoint_cannot_be_weakened(artifact, binding):
    raw = artifact.model_dump(mode="json")
    raw["checkpoint"] = raw["checkpoint"][:1]
    with pytest.raises(Stop, match="checkpoint_mismatch"):
        Policy(binding, "http://localhost:9000").check_artifact(Capability.model_validate(raw))


def test_schema_export_is_real_json_schema():
    raw = json.dumps(Capability.model_json_schema())
    assert "discriminator" in raw and "additionalProperties" in raw


def test_role_and_condition_shape():
    with pytest.raises(ValidationError):
        Target(kind="role", name="x")
    with pytest.raises(ValidationError):
        Condition(target="x", kind="equals_input")


def test_invalid_configured_identifier_pattern_is_a_configuration_error():
    with pytest.raises(ValueError, match="invalid identifier pattern"):
        Input(kind="identifier", pattern="[")


@pytest.mark.parametrize(
    "condition",
    [
        Condition(target="unknown"),
        Condition(target="search_screen", kind="equals_input", input="unknown"),
    ],
)
def test_goal_checkpoint_must_use_product_and_input_vocabulary(binding, condition):
    from computer_use_replay.contracts import GoalRequest

    request = GoalRequest.load(Path("requests/read_savings.json"))
    with pytest.raises(Stop, match="undeclared_checkpoint"):
        Policy(binding, "http://localhost").check_request(
            request.model_copy(update={"checkpoint": (*request.checkpoint, condition)})
        )


@pytest.mark.parametrize("source", [None, "unknown", "search"])
def test_goal_outputs_need_a_permitted_source(binding, source):
    from computer_use_replay.contracts import GoalRequest

    request = GoalRequest.load(Path("requests/read_savings.json"))
    output = request.outputs["available_balance"].model_copy(update={"source": source})
    with pytest.raises(Stop, match="output_contract_mismatch"):
        Policy(binding, "http://localhost").check_request(
            request.model_copy(update={"outputs": {"available_balance": output}})
        )


def test_wrong_field_cannot_satisfy_declared_output(artifact, binding):
    raw = artifact.model_dump(mode="json")
    raw["steps"][-1]["target"] = "member_identity"
    with pytest.raises(ValueError, match="output source mismatch"):
        Capability.model_validate(raw)
    changed = artifact.model_copy(
        update={
            "steps": (
                *artifact.steps[:-1],
                Read(target="member_identity", output="available_balance"),
            )
        }
    )
    with pytest.raises(Stop, match="output_source_mismatch"):
        Policy(binding, "http://localhost").check_artifact(changed)


@pytest.mark.parametrize("raw", ["$ 1,490.00", "$\u00a01,490.00"])
def test_money_accepts_single_currency_spacing(raw):
    assert Output(kind="money").parse(raw) == {"amount": "1490.00", "currency": "USD"}


@pytest.mark.parametrize("raw", ["$  1.00", "$\n1.00", "$\t1.00", "$ 01.00", "$ 1,49.00"])
def test_currency_spacing_does_not_relax_number_format(raw):
    with pytest.raises(ValueError):
        Output(kind="money").parse(raw)


def test_default_commit_preserves_existing_serialization_and_digest(binding):
    raw = binding.model_dump(mode="json")
    assert all("commit_key" not in c for c in raw["controls"].values())
    assert Binding.model_validate(raw).digest() == binding.digest()
    explicit = json.loads(json.dumps(raw))
    explicit["controls"]["member_input"]["commit_key"] = None
    assert Binding.model_validate(explicit).digest() == binding.digest()
    raw["controls"]["member_input"]["commit_key"] = "Tab"
    assert Binding.model_validate(raw).digest() != binding.digest()


@pytest.mark.parametrize("key", ["Enter", "Escape", "Control+s", "", True])
def test_commit_keys_do_not_allow_submission_or_arbitrary_shortcuts(binding, key):
    raw = binding.controls["member_input"].model_dump()
    with pytest.raises(ValueError):
        Control.model_validate({**raw, "commit_key": key})


def test_commit_requires_fill_operation(binding):
    raw = binding.model_dump(mode="json")
    raw["controls"]["search"]["commit_key"] = "Tab"
    with pytest.raises(ValueError, match="commit key requires a fill control"):
        Binding.model_validate(raw)


def test_presentation_overlay_cannot_change_commit_semantics(binding, tmp_path):
    path = tmp_path / "overlay.json"
    payload = {
        "product": binding.product,
        "tenant": "test",
        "targets": {
            "member_input": binding.controls["member_input"].target.model_dump(mode="json")
        },
    }
    path.write_text(json.dumps(payload))
    assert binding.overlay(path).digest() == binding.digest()
    payload["targets"]["member_input"]["commit_key"] = "Tab"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="commit_key"):
        binding.overlay(path)


@pytest.mark.parametrize(
    "text", ["First line\nSecond line", "First\n\nThird", "Unicode — résumé\n🙂"]
)
def test_multiline_preserves_explicit_paragraph_breaks(text):
    assert Input(kind="multiline").validate_value(text) == text
    with pytest.raises(ValueError, match="input_type_mismatch"):
        Input(kind="text").validate_value(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "\n",
        " a",
        "a ",
        "\nfirst",
        "last\n",
        "a\r\nb",
        "a\tb",
        "a  b",
        "a\x00b",
        True,
        "🙂🙂🙂",
    ],
)
def test_multiline_rejects_lossy_or_out_of_budget_text(text):
    with pytest.raises(ValueError, match="input_type_mismatch"):
        Input(kind="multiline", max_length=5).validate_value(text)


@pytest.mark.parametrize("expected", [False, True])
@pytest.mark.parametrize("actual", [None, "False", "True", 0, 1, False, True])
def test_boolean_comparison_requires_real_checked_state(expected, actual):
    condition = Condition(target="flag", kind="equals_input", input="enabled")
    assert condition.matches_value(actual, {"enabled": expected}) == (
        type(actual) is bool and actual is expected
    )


def test_paragraph_mode_changes_contract_but_default_preserves_digest(binding, tmp_path):
    import json

    from computer_use_replay.policy import Binding

    raw = binding.model_dump(mode="json")
    assert all("text_mode" not in c for c in raw["controls"].values())
    assert Binding.model_validate(raw).digest() == binding.digest()
    raw["controls"]["member_input"]["text_mode"] = "paragraphs"
    assert Binding.model_validate(raw).digest() != binding.digest()
    overlay = {
        "product": binding.product,
        "tenant": "test",
        "targets": {
            "member_input": binding.controls["member_input"].target.model_dump(mode="json")
        },
    }
    path = tmp_path / "overlay.json"
    path.write_text(json.dumps(overlay))
    assert binding.overlay(path).digest() == binding.digest()
    overlay["targets"]["member_input"]["text_mode"] = "paragraphs"
    path.write_text(json.dumps(overlay))
    with pytest.raises(ValueError, match="text_mode"):
        binding.overlay(path)


def test_submission_evidence_matches_current_runtime():
    from scripts.preflight import check_evidence

    check_evidence(Path("evidence"))


@pytest.mark.parametrize(
    "fault", ["recorded_model_calls", "replay_uses_model", "missing_failure_image", "unlisted_file"]
)
def test_submission_evidence_rejects_incomplete_or_misleading_records(tmp_path, fault):
    import hashlib
    import shutil

    from scripts.preflight import check_evidence

    root = tmp_path / "evidence"
    shutil.copytree("evidence", root)
    manifest = json.loads((root / "manifest.json").read_text())
    if fault == "missing_failure_image":
        (root / "failure/failure-masked.png").unlink()
    elif fault == "unlisted_file":
        (root / "unexpected-transcript.txt").write_text("not part of the reviewed sample")
    else:
        role = "discovery" if fault == "recorded_model_calls" else "replay"
        path = root / role / "result.json"
        result = json.loads(path.read_text())
        result["llm_calls"] = 0 if role == "discovery" else 1
        path.write_text(json.dumps(result))
        # Recompute the digest: semantic checks must reject this even with matching hashes.
        manifest["files"][f"{role}/result.json"] = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest["runs"][role]["llm_calls"] = result["llm_calls"]
        (root / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises((AssertionError, FileNotFoundError)):
        check_evidence(root)


# The optional Provenance.accepted_by field (added for goal-only onboarding) must
# be invisible to every already-committed capability artifact: same bytes when
# reloaded, same digest. Digests captured once, right after the field was added.
# The erpnext entry has been re-pinned twice, both times because
# integrations/erpnext/profile.json changed and that changed the reviewed
# binding's own digest, so the previous artifact no longer validated against
# it. First when the profile gained the customer_not_found/item_not_found
# business states, then again when it gained one discard rule for Frappe's
# periodic update-check request (a live target's own background traffic, not
# a code change -- see the integration README). Rather than hand-editing the
# genuinely learned artifact's binding_sha256 (which would break its
# provenance and its link to the recorded discovery evidence), the capability
# was genuinely re-learned each time; this is the current artifact's own
# digest (qwen3.6:35b-a3b, 13 native tool calls, both times). The original
# 12-call gemma4:e4b recording is retained under
# integrations/erpnext/evidence/original/ for history. The other three
# entries are untouched by either change.
_COMMITTED_CAPABILITY_DIGESTS = {
    "capabilities/read_savings.json": (
        "d8cf54675b594c085078a1578100f1249520a24e5f0298df4c6485b83d5b2239"
    ),
    "capabilities/prepare_subaccount.json": (
        "9908a4b500470b0c392432733153e8b296e774b99789fc5aa3e05f1cd54fd830"
    ),
    "integrations/erpnext/prepare_quotation.json": (
        "c4b1c9d9a12a8ee5397b9df9f49434fd1cf7a86d1cadcab04d84040bc6e672ef"
    ),
    "integrations/meridian/read_savings.json": (
        "4ebebe1814dae6f792574edf3f8d0475ac66d23136a79d6665e4bcebdfe8ee4a"
    ),
}


@pytest.mark.parametrize("path", sorted(_COMMITTED_CAPABILITY_DIGESTS))
def test_committed_capability_digests_are_unchanged_by_the_optional_acceptance_field(path):
    raw = Path(path).read_bytes()
    capability = Capability.model_validate_json(raw)
    assert capability.digest() == _COMMITTED_CAPABILITY_DIGESTS[path]
    # Bytes on disk stay byte-identical: reloading and re-dumping reproduces the
    # exact same JSON value, and the new field never appears unless it is set.
    assert capability.model_dump(mode="json") == json.loads(raw)
    assert "accepted_by" not in raw.decode()
    assert len(capability.digest()) == 64


def test_provenance_accepted_by_is_omitted_unless_set(artifact):
    assert "accepted_by" not in artifact.model_dump(mode="json")["provenance"]
    accepted = artifact.model_copy(
        update={"provenance": artifact.provenance.model_copy(update={"accepted_by": "flag"})}
    )
    assert accepted.model_dump(mode="json")["provenance"]["accepted_by"] == "flag"
    assert accepted.digest() != artifact.digest()
    assert Capability.model_validate_json(accepted.model_dump_json()) == accepted


@pytest.mark.parametrize("accepted_by", ["flag", "tty"])
def test_provenance_accepts_only_declared_acceptance_methods(accepted_by):
    Provenance(mode="llm", model="m", calls=1, run_id="r", accepted_by=accepted_by)


def test_provenance_rejects_an_undeclared_acceptance_method():
    with pytest.raises(ValidationError):
        Provenance(mode="llm", model="m", calls=1, run_id="r", accepted_by="email")


def test_goal_request_review_is_omitted_when_absent():
    assert "review" not in TASK.model_dump(mode="json")


def test_goal_request_review_round_trips_when_present():
    review = Review(status="draft", proposed_by="ollama/qwen3.6:35b-a3b", goal=TASK.goal)
    draft = TASK.model_copy(update={"review": review})
    dumped = draft.model_dump(mode="json")
    assert dumped["review"] == {
        "status": "draft",
        "proposed_by": "ollama/qwen3.6:35b-a3b",
        "goal": TASK.goal,
    }
    assert GoalRequest.model_validate(dumped) == draft


def test_review_requires_a_recognized_status():
    with pytest.raises(ValidationError):
        Review(status="pending", proposed_by="ollama/x", goal="g")


def test_success_warnings_are_omitted_when_empty_so_old_results_stay_byte_identical():
    # No fallback ran: every result.json committed before the verified ladder
    # existed must still dump exactly like this, warnings key absent entirely.
    result = Success(outputs={"a": "1"}, llm_calls=0)
    dumped = result.model_dump(mode="json")
    assert "warnings" not in dumped
    assert dumped == {"status": "success", "outputs": {"a": "1"}, "llm_calls": 0}


def test_success_warnings_present_when_a_fallback_ran():
    result = Success(outputs={}, llm_calls=0, warnings=("fallback_resolved:search:normalized",))
    dumped = result.model_dump(mode="json")
    assert dumped["warnings"] == ["fallback_resolved:search:normalized"]
