"""Condition types, identity matches, output phases, and grouped checkpoints."""

import json

import pytest

from computer_use_replay.contracts import Condition, Input, Target
from computer_use_replay.control import Handoff, Ownership
from computer_use_replay.engine import Execution, Outcome
from computer_use_replay.evidence import Evidence, Snapshot
from computer_use_replay.planner import action_tools
from computer_use_replay.policy import Binding, Policy, Stop


@pytest.mark.parametrize(
    "display,expected",
    [
        ("2", 2),
        ("2.000", 2),
        (" 2.0 ", 2),
        ("+2.00", 2),
        ("-2.000", -2),
        ("0.000", 0),
        ("-0.0", 0),
        ("1,234.000", 1234),
        ("1234.00", 1234),
        ("123456789012345678901234567890.000", 123456789012345678901234567890),
    ],
)
def test_exact_integer_numeric_comparison(display, expected):
    c = Condition(target="quantity", kind="equals_integer_input", input="quantity")
    assert c.matches_value(display, {"quantity": expected})


@pytest.mark.parametrize(
    "display",
    [
        "",
        "NaN",
        "Infinity",
        "2.01",
        "2e0",
        "02",
        "2,00",
        "2,000",
        "2 000",
        "2,000.1",
        "2.",
        ".2",
        "$2.00",
        "٢",
        "2\n0",
        "2." + "0" * 1000,
    ],
)
def test_numeric_comparison_rejects_wrong_or_unsupported_values(display):
    c = Condition(target="quantity", kind="equals_integer_input", input="quantity")
    assert not c.matches_value(display, {"quantity": 2})


@pytest.mark.parametrize("expected", ["2", True, 2.0])
def test_numeric_checkpoint_never_coerces_caller_types(expected):
    c = Condition(target="quantity", kind="equals_integer_input", input="quantity")
    assert not c.matches_value("2.000", {"quantity": expected})


def test_identifier_equality_retains_leading_zero_and_format_semantics():
    c = Condition(target="id", kind="equals_input", input="id")
    assert c.matches_value("00123", {"id": "00123"})
    assert not c.matches_value("123", {"id": "00123"})
    assert not c.matches_value("2.000", {"id": "2"})


@pytest.mark.parametrize("value", [0, 10, True, "2", 2.0])
def test_integer_bounds_and_types(value):
    with pytest.raises(ValueError):
        Input(kind="integer", minimum=1, maximum=9).validate_value(value)


def test_integer_bounds_are_inclusive_and_optional():
    spec = Input(kind="integer", minimum=1, maximum=9)
    assert spec.validate_value(1) == 1
    assert spec.validate_value(9) == 9
    assert Input(kind="integer").validate_value(-100) == -100
    assert Input(kind="integer", minimum=1).validate_value(100) == 100
    assert Input(kind="integer", maximum=1).validate_value(-100) == -100
    assert "minimum" not in Input(kind="integer").model_dump()
    assert "maximum" not in Input(kind="integer").model_dump()
    assert spec.model_dump()["minimum"] == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": "text", "minimum": 1},
        {"kind": "integer", "minimum": 3, "maximum": 2},
        {"kind": "integer", "minimum": True},
    ],
)
def test_invalid_bounds(kwargs):
    with pytest.raises(ValueError):
        Input(**kwargs)


def test_numeric_condition_requires_explicit_integer_contract(binding, capability):
    from computer_use_replay.policy import Binding, Policy, Stop

    c = Condition(target="balance", kind="equals_integer_input", input="member_id")
    with pytest.raises(ValueError, match="integer input"):
        c.check_input_type({})
    with pytest.raises(ValueError, match="integer input"):
        c.check_input_type(binding.input_types)
    c.check_input_type({"member_id": Input(kind="integer")})
    raw = binding.model_dump(mode="json")
    raw["invariants"].append(c.model_dump(mode="json"))
    with pytest.raises(ValueError, match="integer input"):
        Binding.model_validate(raw)
    request = capability.model_copy(update={"checkpoint": (*capability.checkpoint, c)})
    with pytest.raises(Stop, match="checkpoint_type_mismatch"):
        Policy(binding, "http://localhost").check_request(request)
    with pytest.raises(ValueError, match="integer input"):
        type(capability).model_validate(request.model_dump())


@pytest.mark.parametrize(
    "spec", [Input(kind="text"), Input(kind="integer"), Input(kind="integer", minimum=-1)]
)
def test_count_requires_nonnegative_integer_contract(spec):
    c = Condition(target="rows", kind="count_equals_input", input="count")
    with pytest.raises(ValueError):
        c.check_input_type({"count": spec})


def test_count_accepts_zero_or_positive_bound_and_requires_parameter():
    c = Condition(target="rows", kind="count_equals_input", input="count")
    c.check_input_type({"count": Input(kind="integer", minimum=0)})
    c.check_input_type({"count": Input(kind="integer", minimum=2, maximum=2)})
    with pytest.raises(ValueError):
        Condition(target="rows", kind="count_equals_input")


def changed(binding, name):
    controls = dict(binding.controls)
    controls["search"] = controls["search"].model_copy(
        update={"target": Target(kind="css", name="button"), "match_input": name}
    )
    return binding.model_copy(update={"controls": controls})


@pytest.mark.parametrize("bad", ["undefined_input", None])
def test_matching_requires_declared_parameter_and_css_scope(binding, bad):
    raw = changed(binding, bad or "member_id").model_dump(mode="json")
    if bad is None:
        raw["controls"]["search"]["target"] = {"kind": "role", "role": "button", "name": "Search"}
    with pytest.raises(ValueError):
        Binding.model_validate(raw)


def test_missing_match_parameter_rejected_before_replay(binding, capability):
    modified = changed(binding, "nickname")
    artifact = capability.model_copy(update={"binding_sha256": modified.digest()})
    with pytest.raises(Stop, match="contract_mismatch"):
        Policy(modified, "http://localhost").check_artifact(artifact)


def test_match_reference_is_contract_not_presentation(binding, tmp_path):
    modified = changed(binding, "member_id")
    assert modified.digest() != changed(binding, "nickname").digest()
    path = tmp_path / "overlay.json"
    path.write_text(
        json.dumps(
            {
                "product": binding.product,
                "tenant": "variant",
                "targets": {"search": {"kind": "css", "name": '[role="option"]'}},
            }
        )
    )
    assert modified.overlay(path).digest() == modified.digest()
    raw = json.loads(path.read_text())
    raw["targets"]["search"]["match_input"] = "nickname"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        modified.overlay(path)


@pytest.mark.parametrize("target", ["balance", "search"])
def test_request_cannot_require_unbound_output_or_checkpoint(binding, capability, target):
    from computer_use_replay.contracts import Condition

    controls = dict(binding.controls)
    controls[target] = controls[target].model_copy(
        update={"target": Target(kind="css", name=".requested"), "match_input": "nickname"}
    )
    modified = binding.model_copy(update={"controls": controls})
    request = capability.model_copy(
        update={"checkpoint": (*capability.checkpoint, Condition(target=target))}
    )
    with pytest.raises(Stop, match="contract_mismatch"):
        Policy(modified, "http://localhost").check_request(request)


def test_scope_requires_css_and_bound_input(binding):
    from computer_use_replay.contracts import TargetScope

    scope = TargetScope(container=".row", anchor="a", attribute="data-name")
    with pytest.raises(ValueError, match="CSS targets"):
        Target(kind="label", name="Quantity", scope=scope)
    raw = binding.model_dump(mode="json")
    raw["controls"]["search"]["target"] = Target(kind="css", name="button", scope=scope).model_dump(
        mode="json"
    )
    with pytest.raises(ValueError, match="matched input"):
        Binding.model_validate(raw)
    raw["controls"]["search"]["match_input"] = "member_id"
    assert Binding.model_validate(raw).controls["search"].target.scope == scope


@pytest.mark.parametrize("attribute", ["", 'data-name"]', "has(foo)", "a b"])
def test_scope_attribute_cannot_contain_selector_syntax(attribute):
    from computer_use_replay.contracts import TargetScope

    with pytest.raises(ValueError):
        TargetScope(container=".row", anchor="a", attribute=attribute)


def test_unscoped_serialization_preserves_historical_target_shape():
    assert Target(kind="css", name="input").model_dump(mode="json") == {
        "frames": [],
        "kind": "css",
        "name": "input",
        "role": None,
    }


def test_replay_rejects_mutations_after_output_collection(binding, capability):
    bad = capability.model_copy(update={"steps": (capability.steps[-1], *capability.steps[:-1])})
    with pytest.raises(Stop, match="output_order"):
        Policy(binding, "http://localhost").check_artifact(bad)


@pytest.mark.parametrize(
    "ready,collected,expected",
    [
        (False, [], {"click_control", "fill_control"}),
        (True, [], {"read_control"}),
        (True, ["value"], {"read_control"}),
    ],
)
def test_model_tools_follow_verified_output_phase(ready, collected, expected):
    context = {
        "checkpoint_ready": ready,
        "collected_outputs": collected,
        "observation": {"controls": [{"target": "field", "count": 1}]},
        "catalog": {
            "field": {
                "operations": ["click", "fill", "read"],
                "risk": "reversible",
                "allowed_inputs": ["arg"],
            }
        },
        "inputs": {"arg": {}},
        "outputs": {"value": {"source": "field"}},
    }
    names = {tool["function"]["name"] for tool in action_tools(context)} - {
        "finish",
        "request_help",
    }
    assert names == expected


@pytest.mark.parametrize(
    "ready,collected,allowed",
    [(False, ["value"], False), (True, [], False), (True, ["value"], True)],
)
def test_finish_requires_verified_outputs(ready, collected, allowed):
    tools = action_tools(
        {
            "checkpoint_ready": ready,
            "collected_outputs": collected,
            "outputs": {"value": {}},
            "inputs": {},
            "observation": {"controls": []},
            "catalog": {},
        }
    )
    assert ("finish" in {t["function"]["name"] for t in tools}) is allowed


CONDITIONS = (Condition(target="member_screen"), Condition(target="accounts_screen"))


class Surface:
    def __init__(self, ready=(), *, alternate=False, dialog=False):
        self.ready = set(ready)
        self.alternate = alternate
        self.dialog = dialog
        self.observations = 0
        self.reads = 0

    def check_health(self):
        pass

    async def observe(self):
        self.observations += 1
        if self.alternate:
            self.ready = {CONDITIONS[(self.observations - 1) % 2].target}
        return Snapshot(controls=(), states=(), unknown_dialogs=int(self.dialog))

    async def condition(self, condition, arguments):
        self.reads += 1
        return condition.target in self.ready


def execution(tmp_path, binding, surface, operator=None):
    evidence = Evidence(tmp_path)
    return Execution(
        surface,
        Policy(binding.model_copy(update={"step_timeout": 0.02}), "http://localhost"),
        evidence,
        Handoff(Ownership(evidence), operator),
    )


async def test_ready_group_uses_one_full_observation(tmp_path, binding):
    surface = Surface([c.target for c in CONDITIONS])
    ex = execution(tmp_path, binding, surface)
    await ex.await_effect(CONDITIONS, {}, 4)
    assert surface.observations == 1
    assert surface.reads == 2


async def test_conditions_true_in_different_passes_do_not_complete(tmp_path, binding):
    surface = Surface(alternate=True)
    ex = execution(tmp_path, binding, surface)
    with pytest.raises(Stop, match="checkpoint_failed") as error:
        await ex.await_effect(CONDITIONS, {}, 4)
    assert error.value.target in {c.target for c in CONDITIONS}
    assert surface.observations >= 2


async def test_group_failure_names_the_unsatisfied_member(tmp_path, binding):
    ex = execution(tmp_path, binding, Surface(["member_screen"]))
    with pytest.raises(Stop) as error:
        await ex.await_effect(CONDITIONS, {}, 4)
    assert error.value.target == "accounts_screen"
    assert error.value.expected == "accounts_screen"


async def test_group_handoff_requires_every_condition_before_resume(tmp_path, binding):
    surface = Surface()

    async def operator(owner, request, validate):
        lease = await owner.claim(request)
        surface.ready.add("member_screen")
        with pytest.raises(Stop, match="resume_condition_unmet"):
            await owner.resume(lease, validate)
        assert owner.owner == "human"
        surface.ready.add("accounts_screen")
        await owner.resume(lease, validate)

    ex = execution(tmp_path, binding, surface, operator)
    await ex.await_effect(CONDITIONS, {}, 4)
    assert ex.handoff.ownership.owner == "automation"


async def test_group_still_checks_unexpected_dialogs_first(tmp_path, binding):
    surface = Surface([c.target for c in CONDITIONS], dialog=True)
    ex = execution(tmp_path, binding, surface)
    with pytest.raises(Stop, match="operator_unavailable"):
        await ex.await_effect(CONDITIONS, {}, 4)
    assert surface.reads == 0


async def test_empty_group_still_observes_surface_health(tmp_path, binding):
    surface = Surface()
    ex = execution(tmp_path, binding, surface)
    await ex.settle(())
    assert surface.observations == 1


class StateSurface:
    """Fake surface for settle()'s multi-state precedence, driven off the real
    juniper binding's own declared states/codes: `visible` is the set of state
    codes currently "shown" (their target control present). Clicking a declared
    interstitial's recovery control removes that state's code from `visible`,
    exactly like a real DOM clearing -- so a genuine recovery can be observed to
    have happened, not just asserted about.
    """

    def __init__(self, binding, visible):
        self.binding = binding
        self.visible = set(visible)
        self.performed = []

    def check_health(self):
        pass

    async def observe(self):
        # Binding declaration order on purpose: settle() must not depend on it.
        ordered = tuple(code for code in self.binding.states if code in self.visible)
        return Snapshot(controls=(), states=ordered)

    async def condition(self, condition, arguments):
        for code, state in self.binding.states.items():
            if state.target == condition.target:
                present = code in self.visible
                return (not present) if condition.kind == "absent" else present
        return False

    async def perform(self, step, arguments):
        self.performed.append(step.target)
        for code, state in self.binding.states.items():
            if state.recovery == step.target:
                self.visible.discard(code)


# member_not_found (business) is declared BEFORE service_unavailable (failure),
# session_expired (human) and service_notice (interstitial) in profiles/juniper.json
# -- so `code = snapshot.states[0]` picked the business outcome every time,
# regardless of what else was showing. These three assert the ranking is by KIND.


async def test_state_precedence_failure_over_business(tmp_path, binding):
    surface = StateSurface(binding, {"member_not_found", "service_unavailable"})
    ex = execution(tmp_path, binding, surface)
    with pytest.raises(Stop, match="service_unavailable"):
        await ex.settle()
    assert surface.performed == []


async def test_state_precedence_human_over_business(tmp_path, binding):
    surface = StateSurface(binding, {"member_not_found", "session_expired"})
    ex = execution(tmp_path, binding, surface)
    # No operator attached: escalating to a person surfaces as operator_unavailable.
    with pytest.raises(Stop, match="operator_unavailable"):
        await ex.settle()
    assert surface.performed == []


async def test_state_precedence_interstitial_clears_before_business_is_reported(tmp_path, binding):
    surface = StateSurface(binding, {"member_not_found", "service_notice"})
    ex = execution(tmp_path, binding, surface)
    with pytest.raises(Outcome) as error:
        await ex.settle()
    assert error.value.code == "member_not_found"
    # The interstitial's own recovery control was actually clicked -- the
    # business outcome was reported only once it was genuinely gone, not
    # because it happened to sort first in the binding's declaration.
    assert surface.performed == ["dismiss_notice"]


@pytest.mark.parametrize(
    "visible",
    [
        {"validation_error", "member_not_found"},
        {"member_not_found", "validation_error"},
    ],
)
async def test_state_precedence_ties_within_a_kind_keep_declaration_order(
    tmp_path, binding, visible
):
    # Both are "business"; only declaration order in the binding (member_not_found
    # comes before validation_error in profiles/juniper.json) should break the tie
    # -- the order `visible` is constructed in here must not matter.
    surface = StateSurface(binding, visible)
    ex = execution(tmp_path, binding, surface)
    with pytest.raises(Outcome) as error:
        await ex.settle()
    assert error.value.code == "member_not_found"
