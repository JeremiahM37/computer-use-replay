"""Network grants, action receipts, and rejection of malformed authority."""

import json
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

import pytest
from pydantic import ValidationError

from computer_use_replay.contracts import Capability, Condition, Input, Output, Provenance
from computer_use_replay.evidence import atomic_json
from computer_use_replay.network import RequestRule
from computer_use_replay.planner import Decision, action_tools, parse_call
from computer_use_replay.policy import Binding, Policy, State, Stop


def test_parameter_bound_rpc():
    rule = RequestRule(
        path=r"/api/method/run_doc_method",
        methods=("POST",),
        body_keys=("method", "docs", "item_idx", "args", "reset_item_details"),
        body_equals={
            "method": "process_item_selection",
            "docs.doctype": "Quotation",
            "docs.__islocal": 1,
        },
    )
    fields = {
        "method": "process_item_selection",
        "docs": json.dumps({"doctype": "Quotation", "__islocal": 1, "party_name": "SYNTHETIC"}),
    }
    assert rule.matches("/api/method/run_doc_method", "POST", "")
    assert rule.permits_body(urlencode(fields))
    for bad in ["save", "submit", "delete"]:
        assert not rule.permits_body(urlencode({**fields, "method": bad}))
    assert not rule.permits_body(
        urlencode({**fields, "docs": json.dumps({"doctype": "Sales Invoice", "__islocal": 1})})
    )
    assert not rule.permits_body(
        urlencode({**fields, "docs": json.dumps({"doctype": "Quotation", "__islocal": 0})})
    )
    assert not rule.permits_body(urlencode(fields) + "&method=save")
    assert not rule.permits_body(urlencode({**fields, "cmd": "save"}))
    assert not rule.permits_body("{}")


def test_query_constraints():
    rule = RequestRule(path="/api/search", query_keys=("txt", "doctype"))
    assert rule.matches("/api/search", "GET", "txt=hello&doctype=Customer")
    assert not rule.matches("/api/search", "POST", "txt=hello")
    assert not rule.matches("/api/search", "GET", "cmd=save")
    assert not rule.matches("/api/search", "GET", "txt=hello&txt=goodbye")
    assert not rule.matches("/api/search/anything", "GET", "")


@pytest.mark.parametrize(
    "body", ["{", "[]", '{"method":"x","method":"y"}', "x=1&x=2", "x=1", "x" * 1048577]
)
def test_malformed_or_unknown_bodies_rejected(body):
    assert not RequestRule(path="/api").permits_body(body)


def test_policy_preserves_legacy_and_limits_optional_requests():
    from pathlib import Path

    from computer_use_replay.policy import Binding, Policy, Stop

    b = Binding.load(Path("profiles/juniper.json"))
    origin = "http://127.0.0.1:9999"
    p = Policy(b, origin)
    with pytest.raises(Stop):
        p.check_url(origin + b.entry + "?txt=x")
    rules = (
        RequestRule(path="/api/search", query_keys=("txt",)),
        RequestRule(
            path="/socket.io/",
            methods=("GET", "POST"),
            query_keys=("transport", "EIO", "sid", "t"),
            discard=True,
        ),
    )
    p.binding = b.model_copy(update={"request_rules": rules})
    assert p.check_network_request(origin + "/api/search?txt=hello", "GET")
    assert not p.check_network_request(origin + "/socket.io/?transport=polling", "POST", "anything")
    assert p.discard_socket("ws://127.0.0.1:9999/socket.io/?transport=websocket")
    assert not p.discard_socket("ws://example.com/socket.io/?transport=websocket")
    for url in [
        origin + "/api/search?txt=a&txt=b",
        origin + "/api/search?cmd=save",
        "http://example.com/api/search",
        origin + "/api/%73earch",
        origin + "/api/../search",
    ]:
        with pytest.raises(Stop):
            p.check_network_request(url, "GET")
    with pytest.raises(Stop):
        p.check_network_request(origin + "/api/search", "GET", "cmd=save")


@pytest.mark.parametrize(
    "changes",
    [
        {"path": "["},
        {"methods": ()},
        {"methods": ("DELETE",)},
        {"body_equals": {"method": "save"}},
        {"redirect_to": ("/menu",), "discard": True},
        {"redirect_to": ("/menu", "/menu")},
        {"redirect_to": ("menu",)},
        {"redirect_to": ("//evil.example.com",)},
    ],
)
def test_invalid_grants_fail_configuration(changes):
    with pytest.raises(ValueError):
        RequestRule(**{"path": "/rpc", **changes})


def test_redirect_grant_is_reviewed_narrowly():
    rule = RequestRule(path="/signon", methods=("POST",), redirect_to=("/menu",))
    assert rule.permits_redirect("/menu")
    assert not rule.permits_redirect("/menu/")
    assert not rule.permits_redirect("/settings")
    # Dropped from the wire form entirely when empty, like the other optional grants.
    assert "redirect_to" not in RequestRule(path="/rpc").model_dump()
    assert RequestRule(path="/rpc").model_dump_json() == RequestRule(path="/rpc").model_dump_json()


def test_binding_rejects_a_redirect_grant_to_an_unreviewed_route():
    raw = json.loads(Path("profiles/juniper.json").read_text())
    raw["request_rules"] = [
        {"path": "/desk/renew", "methods": ["GET"], "redirect_to": ["/desk/nowhere"]}
    ]
    with pytest.raises(ValidationError):
        Binding.model_validate(raw)


def test_binding_accepts_a_redirect_grant_to_a_reviewed_route():
    raw = json.loads(Path("profiles/juniper.json").read_text())
    raw["request_rules"] = [{"path": "/", "methods": ["GET"], "redirect_to": ["/desk/search"]}]
    binding = Binding.model_validate(raw)
    assert binding.request_rules[0].redirect_to == ("/desk/search",)


@pytest.mark.parametrize(
    "doc",
    [
        {"doctype": "Quotation", "__islocal": True},
        {"doctype": "Quotation"},
        {"doctype": "Quotation", "__islocal": 0},
        "not-json",
        42,
    ],
)
def test_nested_constraints_reject_wrong_types_and_missing_identity(doc):
    rule = RequestRule(
        path="/rpc",
        body_keys=("docs",),
        body_equals={"docs.doctype": "Quotation", "docs.__islocal": 1},
    )
    assert not rule.permits_body(json.dumps({"docs": doc}))


def test_json_body_and_nested_duplicate_keys():
    rule = RequestRule(path="/rpc", body_keys=("docs",), body_equals={"docs.doctype": "Quotation"})
    assert rule.permits_body(json.dumps({"docs": {"doctype": "Quotation"}}))
    assert not rule.permits_body(
        json.dumps({"docs": '{"doctype":"Invoice","doctype":"Quotation"}'})
    )


def test_legacy_digest_and_request_checks(binding):
    from pathlib import Path

    from computer_use_replay.policy import Policy, Stop

    recorded = json.loads(Path("capabilities/read_savings.json").read_text())
    assert binding.digest() == recorded["binding_sha256"]
    policy = Policy(binding, "https://example.test")
    assert policy.check_network_request("https://example.test" + binding.entry, "GET")
    assert not policy.discard_socket("https://example.test/socket")
    assert not policy.discard_socket("wss://example.test" + binding.entry)
    rule = RequestRule(path="/socket", discard=True)
    policy.binding = binding.model_copy(update={"request_rules": (rule,)})
    assert policy.discard_socket("wss://example.test/socket")
    with pytest.raises(Stop):
        policy.check_url("https://example.test/socket?" + "&".join("key=value" for _ in range(129)))
    policy.binding = binding.model_copy(update={"request_rules": (rule, rule)})
    with pytest.raises(Stop):
        policy.check_url("https://example.test/socket")


def test_empty_parameters_remain_part_of_the_grant():
    rule = RequestRule(path="/api", query_keys=("txt",), body_keys=("name",))
    assert rule.matches("/api", "GET", "txt=")
    assert not rule.matches("/api", "GET", "cmd=")
    assert rule.permits_body("name=")
    assert not rule.permits_body("cmd=")


def test_rule_roundtrip_and_digest_are_part_of_product_contract(binding):
    from computer_use_replay.policy import Binding

    changed = binding.model_copy(update={"request_rules": (RequestRule(path="/api"),)})
    assert changed.digest() != binding.digest()
    restored = Binding.model_validate_json(changed.model_dump_json())
    assert restored.digest() == changed.digest()
    assert restored.request_rules[0].matches("/api", "GET", "")


@pytest.mark.parametrize("value", ["save", "", "allowed&cmd=save", "allowed.save"])
def test_optional_dispatch_values_reject_unreviewed_functions(value):
    rule = RequestRule(
        path="/search",
        methods=("GET", "POST"),
        query_keys=("query", "txt"),
        body_keys=("query", "txt"),
        query_values={"query": ("allowed",)},
        body_values={"query": ("allowed",)},
    )
    assert rule.matches("/search", "GET", "txt=hello")
    assert rule.matches("/search", "GET", "query=allowed&txt=hello")
    assert rule.permits_body("txt=hello")
    assert rule.permits_body("query=allowed&txt=hello")
    assert not rule.matches("/search", "GET", urlencode({"query": value}))
    assert not rule.permits_body(urlencode({"query": value}))
    assert not rule.permits_body('{"query": true}')
    assert not rule.permits_body('{"query": ["allowed"]}')


@pytest.mark.parametrize("field", ["query_values", "body_values"])
@pytest.mark.parametrize("values", [{"query": ("allowed",)}, {"txt": ()}])
def test_optional_dispatch_configuration_requires_declared_nonempty_values(field, values):
    with pytest.raises(ValueError):
        RequestRule(path="/search", query_keys=("txt",), body_keys=("txt",), **{field: values})


def test_entry_deep_link_uses_same_network_policy(binding):
    from computer_use_replay.policy import Binding

    raw = binding.model_dump(mode="json")
    rule = RequestRule(
        path="/login",
        query_keys=("redirect-to",),
        query_values={"redirect-to": ("/desk/quotation/new-quotation",)},
    )
    raw["request_rules"] = [rule.model_dump(mode="json")]
    raw["entry"] = "/login?redirect-to=/desk/quotation/new-quotation"
    restored = Binding.model_validate(raw)
    assert restored.entry == raw["entry"]
    for entry in [
        "/login?redirect-to=https://elsewhere.test",
        "https://elsewhere.test/login",
        "//elsewhere.test/login",
        "/login#fragment",
        "/%6cogin",
    ]:
        with pytest.raises(ValueError):
            Binding.model_validate({**raw, "entry": entry})
    raw["request_rules"][0]["discard"] = True
    with pytest.raises(ValueError):
        Binding.model_validate(raw)


@pytest.mark.parametrize(
    "receipt",
    [{"target": "missing"}, {"target": "member_input", "kind": "equals_input", "input": "other"}],
)
def test_invalid_fill_receipts(binding, receipt):
    raw = binding.model_dump(mode="json")
    raw["controls"]["member_input"]["after_fill"] = receipt
    with pytest.raises(ValueError):
        Binding.model_validate(raw)


def test_receipt_requires_fill_and_matches_the_actual_parameter(binding):
    raw = binding.model_dump(mode="json")
    raw["controls"]["search"]["after_fill"] = {"target": "member_input"}
    with pytest.raises(ValueError):
        Binding.model_validate(raw)
    raw = binding.model_dump(mode="json")
    raw["input_types"]["other"] = Input(kind="text").model_dump()
    raw["controls"]["member_input"]["allowed_inputs"].append("other")
    raw["controls"]["member_input"]["after_fill"] = Condition(
        target="member_input", kind="equals_input", input="member_id"
    ).model_dump()
    b = Binding.model_validate(raw)
    assert b.digest() != binding.digest()
    assert Binding.model_validate_json(b.model_dump_json()).digest() == b.digest()
    policy = Policy(b, "http://localhost")
    policy.check_fill("member_input", "member_id")
    with pytest.raises(Stop, match="input_target_mismatch"):
        policy.check_fill("member_input", "other")


def test_text_output_and_genuine_provenance():
    assert Output(kind="text").parse("  reference  ") == "reference"
    with pytest.raises(ValueError, match="empty_output"):
        Output(kind="text").parse(" \n ")
    with pytest.raises(ValidationError, match="actual call"):
        Provenance(mode="llm", model="model", calls=0, run_id="test")


@pytest.mark.parametrize(
    "condition",
    [
        {"target": "unknown"},
        {"target": "savings_screen", "kind": "equals_input", "input": "unknown"},
    ],
)
def test_undeclared_checkpoint(capability, condition):
    raw = capability.model_dump(mode="json")
    raw["checkpoint"] = [condition]
    with pytest.raises(ValidationError, match="undeclared condition"):
        Capability.model_validate(raw)


@pytest.mark.parametrize(
    "raw",
    [
        {"target": "expired_screen", "kind": "human"},
        {"target": "notice_screen", "kind": "interstitial"},
    ],
)
def test_state_recovery_required(raw):
    with pytest.raises(ValidationError, match="recovery control"):
        State(**raw)


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda r: r.update(entry="/not-allowed"), "allowed GET"),
        (lambda r: next(iter(r["states"].values())).update(target="unknown"), "undefined state"),
        (
            lambda r: next(s for s in r["states"].values() if s["kind"] == "human").update(
                manual_recovery="search"
            ),
            "human-only",
        ),
        (
            lambda r: next(s for s in r["states"].values() if s["kind"] == "interstitial").update(
                recovery="member_input"
            ),
            "reversible click",
        ),
        (lambda r: r["invariants"][0].update(target="unknown"), "undefined checkpoint"),
    ],
)
def test_bad_binding(binding, mutation, message):
    raw = binding.model_dump(mode="json")
    mutation(raw)
    with pytest.raises(ValidationError, match=message):
        Binding.model_validate(raw)


@pytest.mark.parametrize(
    "origin",
    [
        "file:///tmp",
        "http://user:password@example.com",
        "http://example.com/path",
        "https://example.com?q=1",
    ],
)
def test_origin_rejected(binding, origin):
    with pytest.raises(Stop, match="invalid_origin"):
        Policy(binding, origin)


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("inputs", {}, "contract_mismatch"),
        ("checkpoint", (), "checkpoint_mismatch"),
    ],
)
def test_artifact_contract_cannot_override_policy(binding, capability, field, value, code):
    # model_copy intentionally represents an already-instantiated untrusted object.
    with pytest.raises(Stop, match=code):
        Policy(binding, "http://localhost").check_artifact(
            capability.model_copy(update={field: value})
        )


def test_atomic_write_failure_preserves_previous_file_and_cleans_temp(tmp_path):
    path = tmp_path / "capability.json"
    atomic_json(path, {"version": "old"})
    previous = path.read_bytes()
    with patch("computer_use_replay.evidence.os.replace", side_effect=OSError("disk fault")):
        with pytest.raises(OSError):
            atomic_json(path, {"version": "new"})
    assert path.read_bytes() == previous
    assert list(tmp_path.iterdir()) == [path]


def test_decision_fields_must_match_operation():
    with pytest.raises(ValidationError, match="invalid decision fields"):
        Decision(op="done", target="search", reason="goal_complete")


def test_parse_tools_rejects_unoffered_arguments_and_reads():
    context = {
        "observation": {"controls": [{"target": "balance", "count": 1}]},
        "catalog": {"balance": {"operations": ["read"], "risk": "reversible"}},
        "inputs": {},
        "outputs": {"amount": {}},
    }
    offered = action_tools(context)

    def call(name, args):
        return parse_call({"function": {"name": name, "arguments": args}}, offered)

    assert call("request_help", {}).op == "stop"
    assert call("read_control", {"target": "balance", "output": "amount"}).output == "amount"
    with pytest.raises(ValueError, match="arguments mismatch"):
        call("read_control", {})
    with pytest.raises(ValueError, match="unoffered argument"):
        call("read_control", {"target": "unseen", "output": "amount"})


@pytest.mark.parametrize(
    "field,value", [("binding_sha256", "0" * 64), ("product", "another_product")]
)
def test_artifact_bound_to_exact_reviewed_installation(binding, capability, field, value):
    with pytest.raises(Stop, match="binding_mismatch"):
        Policy(binding, "http://localhost").check_artifact(
            capability.model_copy(update={field: value})
        )


def test_fill_tool_references_parameter_not_literal():
    context = {
        "observation": {"controls": [{"target": "identifier", "count": 1}]},
        "catalog": {"identifier": {"operations": ["fill"], "risk": "reversible"}},
        "inputs": {"member_id": {}},
        "outputs": {},
    }
    offered = action_tools(context)
    decision = parse_call(
        {
            "function": {
                "name": "fill_control",
                "arguments": {"target": "identifier", "parameter": "member_id"},
            }
        },
        offered,
    )
    assert decision.op == "fill" and decision.input == "member_id"
    with pytest.raises(ValueError, match="unoffered argument"):
        parse_call(
            {
                "function": {
                    "name": "fill_control",
                    "arguments": {"target": "identifier", "parameter": "00123"},
                }
            },
            offered,
        )


def test_unknown_input_kind_cannot_validate_even_if_constructor_is_bypassed():
    from computer_use_replay.contracts import Input

    with pytest.raises(ValueError, match="input_type_mismatch"):
        Input.model_construct(kind="future_unknown_kind").validate_value("00123")


def test_presentation_drift_is_empty_for_the_recording_tenant_and_lists_every_relabeled_key(
    binding, capability
):
    # `capability` is built directly from the binding's own targets: the current
    # presentation resolves every key exactly as recorded.
    assert Policy(binding, "http://localhost").presentation_drift(capability) == ()
    overlay = binding.overlay(Path("profiles/tenant_b.json"))
    drifted = Policy(overlay, "http://localhost").presentation_drift(capability)
    # tenant_b.json relabels three controls and renames the "workbench" frame; every
    # target lives in that frame, so the frame-lineage change touches every key, not
    # just the three relabeled ones.
    assert drifted == tuple(sorted(capability.targets))
    assert drifted == tuple(sorted(set(drifted)))  # unique keys, sorted


def test_presentation_drift_only_reports_keys_the_artifact_actually_recorded(binding, capability):
    overlay = binding.overlay(Path("profiles/tenant_b.json"))
    narrowed = capability.model_copy(update={"targets": {"search": capability.targets["search"]}})
    assert Policy(overlay, "http://localhost").presentation_drift(narrowed) == ("search",)


def test_alternates_are_excluded_from_the_policy_fingerprint(binding):
    # Same rule as the primary target (see digest()): a reviewed fallback rung is
    # presentation, never product policy, so adding, reordering or removing one
    # never moves the fingerprint that pins a saved artifact's binding_sha256.
    controls = dict(binding.controls)
    locate_member = controls["search"].target.model_copy(update={"name": "Locate member"})
    controls["search"] = controls["search"].model_copy(update={"alternates": (locate_member,)})
    with_alternate = binding.model_copy(update={"controls": controls})
    assert with_alternate.digest() == binding.digest()
    find_a_member = controls["search"].target.model_copy(update={"name": "Find a member"})
    controls["search"] = controls["search"].model_copy(
        update={"alternates": (find_a_member, locate_member)}
    )
    reordered = binding.model_copy(update={"controls": controls})
    assert reordered.digest() == binding.digest()


def test_overlay_can_add_alternates_only_for_declared_controls(binding, tmp_path):
    path = tmp_path / "overlay.json"
    payload = {
        "product": binding.product,
        "tenant": "test",
        "alternates": {
            "search": [
                binding.controls["search"].target.model_dump(mode="json")
                | {"name": "Locate member"}
            ]
        },
    }
    path.write_text(json.dumps(payload))
    overlaid = binding.overlay(path)
    assert [t.name for t in overlaid.controls["search"].alternates] == ["Locate member"]
    # Presentation-layer only: adding a reviewed alternate never moves the fingerprint.
    assert overlaid.digest() == binding.digest()
    payload["alternates"] = {"undeclared_control": payload["alternates"]["search"]}
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="incompatible presentation"):
        binding.overlay(path)


def test_overlay_remaps_alternate_frame_lineage_like_the_primary_target(binding, tmp_path):
    path = tmp_path / "overlay.json"
    payload = {
        "product": binding.product,
        "tenant": "test",
        "frames": {"workbench": "renamed"},
        "alternates": {"search": [binding.controls["search"].target.model_dump(mode="json")]},
    }
    path.write_text(json.dumps(payload))
    overlaid = binding.overlay(path)
    assert overlaid.controls["search"].alternates[0].frames == ("renamed",)


def test_alternate_scope_requires_a_matched_input(binding):
    from computer_use_replay.contracts import Target, TargetScope

    raw = binding.model_dump(mode="json")
    raw["controls"]["search"]["alternates"] = [
        Target(
            kind="css", name=".row input", scope=TargetScope(container=".row", anchor="a")
        ).model_dump(mode="json")
    ]
    with pytest.raises(ValueError, match="container scopes require a matched input"):
        Binding.model_validate(raw)


def test_alternates_default_empty_and_round_trip_serialization(binding):
    raw = binding.model_dump(mode="json")
    assert all("alternates" not in c for c in raw["controls"].values())
    assert Binding.model_validate(raw).digest() == binding.digest()


def test_overlay_can_set_the_fallback_ladder_without_moving_the_fingerprint(binding, tmp_path):
    assert binding.fallback == "verified"
    path = tmp_path / "overlay.json"
    path.write_text(json.dumps({"product": binding.product, "tenant": "test", "fallback": "off"}))
    overlaid = binding.overlay(path)
    assert overlaid.fallback == "off"
    assert overlaid.digest() == binding.digest()


def test_overlay_leaves_fallback_alone_when_not_declared(binding, tmp_path):
    path = tmp_path / "overlay.json"
    path.write_text(json.dumps({"product": binding.product, "tenant": "test"}))
    assert binding.overlay(path).fallback == binding.fallback == "verified"


def test_fallback_is_excluded_from_the_binding_digest():
    raw = Path("profiles/juniper.json").read_text()
    default_digest = Binding.model_validate_json(raw).digest()
    off_digest = Binding.model_validate_json(raw).model_copy(update={"fallback": "off"}).digest()
    assert default_digest == off_digest
