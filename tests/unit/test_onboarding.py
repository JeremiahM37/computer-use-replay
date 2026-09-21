"""Vocabulary construction and strict single-tool-call validation for `propose`.

Transport, retry and end-to-end proposal behavior (including the genuine model
protocol) live in tests/integration/test_onboarding.py; this module only covers
the pure, synchronous pieces: the catalog built from a reviewed binding, and
`parse_proposal_call`'s rejection of anything not already offered.
"""

from pathlib import Path

import pytest

from computer_use_replay.contracts import GoalRequest, Review
from computer_use_replay.onboarding import (
    Proposal,
    confirm_draft,
    describe_draft,
    draft_request,
    parse_proposal_call,
    propose_tool,
    vocabulary,
)
from computer_use_replay.policy import Policy, Stop


@pytest.fixture
def catalog(binding):
    return vocabulary(binding)


@pytest.fixture
def offered(catalog):
    return [propose_tool(catalog)]


def call(name, arguments):
    return {"function": {"name": name, "arguments": arguments}}


VALID_ARGS = {
    "name": "read_savings_auto",
    "inputs": ["member_id"],
    "outputs": [{"name": "available_balance", "source": "balance", "kind": "money"}],
    "success_screen": "savings_screen",
}


def test_vocabulary_excludes_routes_risk_and_page_data(binding, catalog):
    assert set(catalog) == {"inputs", "controls", "outputs", "screens"}
    assert catalog["inputs"]["member_id"] == {"kind": "identifier", "pattern": "[0-9]{5}"}
    assert "balance" in catalog["outputs"] and "savings_screen" in catalog["screens"]
    # Member identifiers/balances never leak into the vocabulary at all.
    dumped = str(catalog)
    assert "00123" not in dumped and "8902.10" not in dumped
    # No control carries risk, routes or invariants into what the model sees.
    for entry in catalog["controls"].values():
        assert set(entry) <= {"label", "operations", "allowed_inputs"}
    # A readable, enum-constrained control (review_status) surfaces its allowed values.
    assert catalog["outputs"]["review_status"]["allowed_values"] == ["Ready for confirmation"]
    # A plain readable control (balance) carries no allowed_values key at all.
    assert "allowed_values" not in catalog["outputs"]["balance"]


def test_propose_tool_enums_are_derived_from_the_catalog(catalog, offered):
    props = offered[0]["function"]["parameters"]["properties"]
    assert props["inputs"]["items"]["enum"] == sorted(catalog["inputs"])
    assert props["outputs"]["items"]["properties"]["source"]["enum"] == sorted(catalog["outputs"])
    assert props["success_screen"]["enum"] == sorted(catalog["screens"])


def test_valid_proposal_parses(offered, catalog):
    proposal = parse_proposal_call(call("propose_contract", VALID_ARGS), offered, catalog)
    assert proposal == Proposal(
        name="read_savings_auto",
        inputs=("member_id",),
        outputs={"available_balance": ("balance", "money")},
        success_screen="savings_screen",
    )


@pytest.mark.parametrize(
    "mutate,message",
    [
        (lambda a: {**a, "name": "Not-A-Slug"}, "invalid capability name"),
        (lambda a: {**a, "inputs": ["not_a_declared_input"]}, "undeclared_input"),
        (lambda a: {**a, "inputs": ["member_id", "member_id"]}, "undeclared_input"),
        (lambda a: {**a, "inputs": []}, "undeclared_input"),
        (
            lambda a: {**a, "outputs": [{"name": "x", "source": "search", "kind": "text"}]},
            "non_readable_output_source",
        ),
        (
            lambda a: {**a, "outputs": [{"name": "x", "source": "not_a_control", "kind": "text"}]},
            "non_readable_output_source",
        ),
        (
            lambda a: {**a, "outputs": [{"name": "x", "source": "balance", "kind": "percentage"}]},
            "invalid_output_kind",
        ),
        (
            lambda a: {
                **a,
                "outputs": [{"name": "Bad Name", "source": "balance", "kind": "money"}],
            },
            "invalid_output_name",
        ),
        (
            lambda a: {
                **a,
                "outputs": [
                    {"name": "x", "source": "balance", "kind": "money"},
                    {"name": "x", "source": "member_identity", "kind": "text"},
                ],
            },
            "invalid_output_name",
        ),
        (lambda a: {**a, "outputs": []}, "missing_outputs"),
        (
            lambda a: {**a, "outputs": [{"name": "x", "source": "balance"}]},
            "malformed_output",
        ),
        (lambda a: {**a, "success_screen": "not_a_screen"}, "missing_screen"),
        (lambda a: {**a, "success_screen": "balance"}, "missing_screen"),
        (lambda a: {k: v for k, v in a.items() if k != "name"}, "tool arguments mismatch"),
        (lambda a: {**a, "extra": 1}, "tool arguments mismatch"),
    ],
)
def test_rejection_paths(offered, catalog, mutate, message):
    with pytest.raises(ValueError, match=message):
        parse_proposal_call(call("propose_contract", mutate(VALID_ARGS)), offered, catalog)


def test_unexpected_tool_name_rejected(offered, catalog):
    with pytest.raises(ValueError, match="unexpected tool call"):
        parse_proposal_call(call("execute_shell", {}), offered, catalog)


def test_non_dict_arguments_rejected(offered, catalog):
    with pytest.raises(ValueError, match="tool arguments mismatch"):
        parse_proposal_call(call("propose_contract", "not-a-dict"), offered, catalog)


def test_draft_request_appends_only_matching_invariants(binding):
    proposal = Proposal(
        name="read_savings_auto",
        inputs=("member_id",),
        outputs={"available_balance": ("balance", "money")},
        success_screen="savings_screen",
    )
    request = draft_request(binding, "Read the balance.", proposal, proposed_by="ollama/test")
    assert request.review == Review(
        status="draft", proposed_by="ollama/test", goal="Read the balance."
    )
    # member_identity invariant references member_id (present); review_nickname's
    # invariant references nickname (absent from this proposal's inputs) and must
    # not be silently pulled in.
    targets = {c.target for c in request.checkpoint}
    assert targets == {"savings_screen", "member_identity"}
    Policy(binding, "http://localhost").check_request(request)


def test_draft_request_never_duplicates_a_repeated_invariant(binding):
    proposal = Proposal(
        name="read_savings_auto",
        inputs=("member_id",),
        outputs={"available_balance": ("balance", "money")},
        success_screen="savings_screen",
    )
    duplicated = binding.model_copy(
        update={"invariants": binding.invariants + (binding.invariants[0],)}
    )
    request = draft_request(duplicated, "Read the balance.", proposal, proposed_by="ollama/test")
    matching = [c for c in request.checkpoint if c.target == "member_identity"]
    assert len(matching) == 1


def test_draft_request_includes_a_declared_allowed_output_values_enum(binding):
    proposal = Proposal(
        name="prepare_subaccount_auto",
        inputs=("member_id", "nickname"),
        outputs={"review_status": ("review_status", "text")},
        success_screen="confirmation_screen",
    )
    request = draft_request(binding, "Prepare a sub-account.", proposal, proposed_by="ollama/test")
    assert request.outputs["review_status"].allowed_values == ("Ready for confirmation",)
    Policy(binding, "http://localhost").check_request(request)


def test_describe_draft_omits_invocation_values(binding):
    proposal = Proposal(
        name="read_savings_auto",
        inputs=("member_id",),
        outputs={"available_balance": ("balance", "money")},
        success_screen="savings_screen",
    )
    request = draft_request(binding, "Read the balance.", proposal, proposed_by="ollama/test")
    text = describe_draft(request)
    assert "read_savings_auto" in text and "ollama/test" in text
    assert "member_id" in text and "available_balance" in text
    assert "00123" not in text


async def test_confirm_draft_fails_closed_off_a_tty(monkeypatch, binding):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    request = GoalRequest.load(Path("requests/read_savings.json"))
    assert await confirm_draft(request) is False


async def test_confirm_draft_accepts_a_y_answer(monkeypatch, binding):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    async def fake_input():
        return "y"

    monkeypatch.setattr("computer_use_replay.onboarding.console_input", fake_input)
    proposal = Proposal(
        name="read_savings_auto",
        inputs=("member_id",),
        outputs={"available_balance": ("balance", "money")},
        success_screen="savings_screen",
    )
    request = draft_request(binding, "Read the balance.", proposal, proposed_by="ollama/test")
    assert await confirm_draft(request) is True


async def test_confirm_draft_declines_anything_but_y(monkeypatch, binding):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    async def fake_input():
        return "no thanks"

    monkeypatch.setattr("computer_use_replay.onboarding.console_input", fake_input)
    proposal = Proposal(
        name="read_savings_auto",
        inputs=("member_id",),
        outputs={"available_balance": ("balance", "money")},
        success_screen="savings_screen",
    )
    request = draft_request(binding, "Read the balance.", proposal, proposed_by="ollama/test")
    assert await confirm_draft(request) is False


async def test_confirm_draft_treats_console_disconnect_as_decline(monkeypatch, binding):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    async def disconnected():
        raise Stop("operator_disconnected")

    monkeypatch.setattr("computer_use_replay.onboarding.console_input", disconnected)
    proposal = Proposal(
        name="read_savings_auto",
        inputs=("member_id",),
        outputs={"available_balance": ("balance", "money")},
        success_screen="savings_screen",
    )
    request = draft_request(binding, "Read the balance.", proposal, proposed_by="ollama/test")
    assert await confirm_draft(request) is False
