"""ERPNext integration: reviewed profile/request/artifact load and stay policy-
compatible offline, mirroring how Meridian Core is checked (test_meridian.py)
and the base juniper profile is checked (test_contracts.py, test_policy.py).
No network access: only Binding/GoalRequest/Capability/Policy validation
against the checked-in JSON files. This is the complex-interaction target --
two autocomplete pickers, a native dropdown and an editable grid row against
unmodified ERPNext 16 -- so its assertions lean on that shape rather than
repeating Meridian's read-only-flow checks verbatim."""

import json
from pathlib import Path

import pytest

from computer_use_replay.contracts import Capability, GoalRequest
from computer_use_replay.policy import Binding, Policy, Stop

ORIGIN = "http://127.0.0.1:18080"


@pytest.fixture
def binding():
    return Binding.load(Path("integrations/erpnext/profile.json"))


@pytest.fixture
def request_task():
    return GoalRequest.load(Path("integrations/erpnext/request.json"))


@pytest.fixture
def artifact():
    return Capability.model_validate_json(
        Path("integrations/erpnext/prepare_quotation.json").read_text()
    )


def test_profile_and_request_load_and_are_policy_compatible(binding, request_task):
    assert binding.product == "erpnext"
    assert binding.entry == "/login?redirect-to=/desk/quotation/new-quotation"
    policy = Policy(binding, ORIGIN)
    policy.check_request(request_task)


def test_committed_artifact_matches_the_reviewed_binding(binding, artifact):
    assert artifact.binding_sha256 == binding.digest()
    assert artifact.provenance.mode == "llm"
    # Re-learned twice after profile.json changed and moved the reviewed
    # binding's digest: once for customer_not_found/item_not_found, again for
    # the discard rule covering Frappe's periodic update-check request (a
    # live target's own background traffic, not a code change -- see the
    # integration README). The original 12-call gemma4:e4b recording is
    # retained under evidence/original/ for history (see its README note) but
    # is no longer the artifact of record.
    assert artifact.provenance.model == "qwen3.6:35b-a3b"
    assert artifact.provenance.calls == 13
    Policy(binding, ORIGIN).check_artifact(artifact)


def test_artifact_exercises_pickers_dropdown_and_grid_before_one_final_read(artifact):
    ops = [step.op for step in artifact.steps]
    # Exactly one read, and it is terminal: nothing follows output collection.
    assert ops.count("read") == 1
    assert ops[-1] == "read"
    assert all(op in ("fill", "click") for op in ops[:-1])
    targets = [step.target for step in artifact.steps]
    # Both autocomplete pickers (fill the input, click the resolved option)...
    assert "customer" in targets and "customer_option" in targets
    assert "item" in targets and "item_option" in targets
    # ...the native order-type dropdown...
    assert "order_type" in targets
    # ...and opening/filling/closing the editable grid row.
    assert "edit_item" in targets and "quantity" in targets and "close_editor" in targets


@pytest.mark.parametrize(
    "url,method,body",
    [
        (ORIGIN + "/login?redirect-to=/desk/quotation/new-quotation", "GET", None),
        (
            ORIGIN + "/api/method/login",
            "POST",
            "cmd=login&usr=Administrator&pwd=x&redirect_to=%2Fdesk%2Fquotation%2Fnew-quotation",
        ),
        (ORIGIN + "/desk", "GET", None),
        (ORIGIN + "/desk/quotation/new-quotation", "GET", None),
        (ORIGIN + "/api/method/frappe.desk.search.search_link", "GET", None),
        (ORIGIN + "/api/method/erpnext.stock.get_item_details.apply_price_list", "POST", None),
        (ORIGIN + "/api/method/erpnext.accounts.party.get_party_details", "POST", None),
        (ORIGIN + "/assets/erpnext/dist/main.bundle.css", "GET", None),
    ],
)
def test_the_reviewed_quotation_flow_routes_are_allowed(binding, url, method, body):
    Policy(binding, ORIGIN).check_network_request(url, method, body)


@pytest.mark.parametrize(
    "url,method",
    [
        (ORIGIN + "/api/method/frappe.client.save", "POST"),  # saving is never granted
        (ORIGIN + "/api/resource/Quotation", "POST"),  # nor is the raw REST create route
        (ORIGIN + "/api/method/frappe.client.delete", "POST"),  # deletion, same reasoning
        (ORIGIN + "/app/quotation", "GET"),  # legacy desk alias; not the reviewed one
        (ORIGIN + "/api/method/frappe.desk.search.search_link", "DELETE"),  # method not granted
        ("https://evil.example.com/desk", "GET"),  # different origin entirely
    ],
)
def test_anything_outside_the_reviewed_quotation_flow_is_denied(binding, url, method):
    with pytest.raises(Stop):
        Policy(binding, ORIGIN).check_network_request(url, method)


def test_only_one_interstitial_state_is_declared_and_its_recovery_is_reversible(binding):
    kinds = {name: state.kind for name, state in binding.states.items()}
    assert kinds == {
        "onboarding_open": "interstitial",
        "customer_not_found": "business",
        "item_not_found": "business",
    }
    onboarding = binding.states["onboarding_open"]
    recovery = binding.controls[onboarding.recovery]
    assert recovery.risk == "reversible" and "click" in recovery.operations


def test_not_found_states_are_declared_business_outcomes_with_no_recovery(binding):
    # Zero visible matches for the requested customer/item is a documented
    # ERPNext outcome, not a handoff or an interstitial to clear -- so neither
    # declared state names a recovery control, matching juniper's
    # member_not_found (see profiles/juniper.json).
    for code, marker in [
        ("customer_not_found", "no_customer_match"),
        ("item_not_found", "no_item_match"),
    ]:
        state = binding.states[code]
        assert state.kind == "business"
        assert state.recovery is None
        assert state.manual_recovery is None
        assert state.target == marker
        marker_control = binding.controls[marker]
        # Passive observation only: the marker itself is never clicked or filled.
        assert marker_control.operations == ()
        assert marker_control.risk == "reversible"


def test_not_found_markers_are_scoped_to_their_own_picker_and_require_the_create_new_row(binding):
    # Each marker keys off the create-new/filter-note row ONLY when it is the
    # dropdown's first (auto-selected) entry -- the row that appears beside a
    # real match too (see integrations/erpnext/evidence/README.md), so a
    # locator that just checked for its presence would misfire on every
    # search. aria-selected="true" is what makes it exclusive to no-match.
    customer_marker = binding.controls["no_customer_match"].target
    assert customer_marker.kind == "css"
    assert 'data-fieldname="party_name"' in customer_marker.name
    assert 'aria-selected="true"' in customer_marker.name
    assert "Create a new Customer" in customer_marker.name

    item_marker = binding.controls["no_item_match"].target
    assert item_marker.kind == "css"
    assert 'data-fieldname="item_code"' in item_marker.name
    assert 'aria-selected="true"' in item_marker.name
    assert "filter_description__link_option" in item_marker.name


def test_save_is_declared_human_only_and_never_appears_in_the_learned_steps(binding, artifact):
    # Quotation preparation must stay unsaved: save exists in the reviewed
    # vocabulary (so a human operator can act on the same page) but is
    # human_only risk, and the model never learned to click it.
    assert binding.controls["save"].risk == "human_only"
    assert "save" not in [step.target for step in artifact.steps]


def test_credentials_are_typed_inputs_never_embedded_in_the_profile(binding):
    assert binding.input_types["username"].kind == "text"
    assert binding.input_types["username"].pattern is None
    assert binding.input_types["password"].kind == "text"
    assert binding.input_types["password"].pattern is None
    dumped = json.dumps(binding.model_dump(mode="json"))
    assert "Administrator" not in dumped
    assert '"admin"' not in dumped


def test_identifier_patterns_match_every_seeded_record_shape(binding):
    customer = binding.input_types["customer_id"]
    for value in ["CP-CUSTOMER-001", "CP-CUSTOMER-012", "CP-CUSTOMER-024"]:
        customer.validate_value(value)
    with pytest.raises(ValueError):
        customer.validate_value("CP-CUSTOMER-1")

    item = binding.input_types["item_id"]
    for value in ["CP-PUMP-100", "CP-PUMP-200", "CP-VALVE-100", "CP-SERVICE-100"]:
        item.validate_value(value)
    with pytest.raises(ValueError):
        item.validate_value("valve-100")

    order_type = binding.input_types["order_type"]
    for value in ["Sales", "Maintenance", "Shopping Cart"]:
        order_type.validate_value(value)
    with pytest.raises(ValueError):
        order_type.validate_value("Purchase")


def test_request_rejects_a_binding_from_a_different_product():
    other = Binding.load(Path("profiles/juniper.json"))
    request_task = GoalRequest.load(Path("integrations/erpnext/request.json"))
    with pytest.raises(Stop):
        Policy(other, ORIGIN).check_request(request_task)
