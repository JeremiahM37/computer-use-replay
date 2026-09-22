"""The live observation wire contract is bounded and value-free."""

import json

import httpx
import pytest

from computer_use_replay.discovery import _surface_live_pin, _surface_live_unpin
from computer_use_replay.evidence import Evidence
from computer_use_replay.planner import ModelPlanner
from computer_use_replay.policy import Stop
from computer_use_replay.providers import ProviderConfig
from tests.provider_samples import response_for


async def test_model_payload_contains_candidate_ids_without_page_values(tmp_path):
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200, json=response_for("ollama", "click_control", {"target": "live_1_safe"})
        )

    planner = ModelPlanner(
        ProviderConfig("ollama", "fixture", "http://model.test", timeout=2),
        Evidence(tmp_path),
        transport=httpx.MockTransport(handler),
    )
    planner.mode = "test_fixture"
    context = {
        "capability": "lookup",
        "goal": "Choose the approved action",
        "checkpoint": [],
        "checkpoint_ready": False,
        "inputs": {"member_id": {"kind": "identifier", "sensitive": True}},
        "outputs": {},
        "observation": {
            "controls": [],
            "states": [],
            "live_candidates": [
                {
                    "candidate_id": "live_1_safe",
                    "scope": "lookup",
                    "kind": "button",
                    "role": "button",
                    "label": "Find member",
                }
            ],
        },
        "catalog": {
            "live_1_safe": {
                "label": "Find member",
                "description": "live scoped control",
                "operations": ("click",),
                "risk": "reversible",
                "allowed_inputs": (),
            }
        },
        "history": [],
        "collected_outputs": [],
    }
    decision = await planner.decide(context)
    assert decision.target == "live_1_safe"
    wire = json.dumps(captured)
    assert "live_1_safe" in wire and "Find member" in wire
    for secret in ("00123", "1204.57", "member_key", "PRIVATE-NOTE"):
        assert secret not in wire


@pytest.mark.parametrize("async_methods", [False, True])
async def test_live_pin_protocol_accepts_sync_and_async_surface_methods(async_methods):
    calls = []

    class Surface:
        def pin_live_candidate(self, candidate_id):
            calls.append(("pin", candidate_id))
            if async_methods:

                async def result():
                    return True

                return result()
            return True

        def unpin_live_candidate(self, candidate_id):
            calls.append(("unpin", candidate_id))
            if async_methods:

                async def result():
                    return None

                return result()

    surface = Surface()
    await _surface_live_pin(surface, "live_1")
    await _surface_live_unpin(surface, "live_1")
    assert calls == [("pin", "live_1"), ("unpin", "live_1")]


async def test_live_pin_protocol_fails_closed_on_rejected_candidate():
    class Surface:
        async def pin_live_candidate(self, _candidate_id):
            return False

    with pytest.raises(Stop) as caught:
        await _surface_live_pin(Surface(), "live_stale")
    assert caught.value.code == "live_candidate_stale"
    assert caught.value.observed == "candidate expired"


async def test_live_form_rejects_unreviewed_hidden_post_field(live):
    """Route permission must not allow an extra, invisible request parameter."""
    from pathlib import Path

    from computer_use_replay.contracts import Click, Condition, Fill
    from computer_use_replay.policy import Binding

    async with live() as (execution, app):
        surface = execution.surface
        execution.policy.binding = Binding.load(Path("profiles/juniper_live.json"))
        await surface.navigate()
        surface.bind_arguments({"member_id": "00123"})
        snapshot = await surface.observe()
        field = next(c for c in snapshot.live_candidates if c.kind == "field")
        await surface.perform(
            Fill(target=field.candidate_id, input="member_id"), {"member_id": "00123"}
        )
        await (
            surface.page.frame(name="workbench")
            .locator("form")
            .evaluate(
                "form => {const extra=document.createElement('input'); extra.type='hidden'; "
                "extra.name='unexpected_operation'; extra.value='delete'; form.append(extra);}"
            )
        )
        completed = []
        surface.page.on(
            "requestfinished", lambda request: completed.append((request.method, request.url))
        )
        snapshot = await surface.observe()
        button = next(c for c in snapshot.live_candidates if c.kind == "button")
        async with surface.page.expect_event("requestfailed") as failed:
            try:
                await surface.perform(
                    Click(target=button.candidate_id, after=Condition(target="results_screen")), {}
                )
            except Stop as stop:
                assert stop.code == "network_policy"
        rejected = await failed.value
        assert rejected.method == "POST" and rejected.url.endswith("/desk/search")
        with pytest.raises(Stop, match="network_policy"):
            surface.check_health()
        assert surface.blocked == "network_policy"
        assert not any(
            method == "POST" and url.endswith("/desk/search") for method, url in completed
        )
        assert not surface._live_pinned_handles
        assert app.state.finalizations == 0


async def test_legacy_surface_needs_no_live_pin_hooks():
    from types import SimpleNamespace

    legacy_surface = SimpleNamespace()
    await _surface_live_pin(legacy_surface, "unused")
    await _surface_live_unpin(legacy_surface, "unused")


async def test_discovery_reobserves_if_selected_control_disappears(live, tmp_path):
    from pathlib import Path

    from computer_use_replay.contracts import GoalRequest
    from computer_use_replay.discovery import discover
    from computer_use_replay.planner import Decision
    from computer_use_replay.policy import Binding

    class VanishingControlPlanner:
        calls = 0
        mode = "test_fixture"
        model = "fixture"

        async def decide(self, context):
            self.calls += 1
            if self.calls > 1:
                return Decision(op="stop", reason="cannot_proceed")
            field = next(
                c for c in context["observation"]["live_candidates"] if c["kind"] == "field"
            )
            return Decision(
                op="fill", target=field["candidate_id"], input="member_id", reason="enter_parameter"
            )

    async with live() as (execution, _):
        execution.policy.binding = Binding.load(Path("profiles/juniper_live.json"))
        surface = execution.surface
        original_pin = surface.pin_live_candidate

        async def pin_then_detach(key):
            await original_pin(key)
            await surface._live_pinned_handles[key].evaluate("element => element.remove()")

        surface.pin_live_candidate = pin_then_detach
        artifact, result = await discover(
            execution,
            VanishingControlPlanner(),
            GoalRequest.load(Path("requests/read_savings.json")),
            {"member_id": "00123"},
            tmp_path / "should-not-exist.json",
        )
        assert artifact is None and result.failure.code == "model_stuck"
        assert not surface._live_pinned_handles
        events = [
            json.loads(line)
            for line in (execution.evidence.directory / "events.jsonl").read_text().splitlines()
        ]
        assert any(event.get("code") == "surface_changed_before_action" for event in events)
        assert not any(event["event"] == "action_started" for event in events)


@pytest.mark.parametrize("fault", ["overlapping_scopes", "wrong_parameter"])
async def test_live_dispatch_enforces_unique_authority_and_parameter_binding(live, fault):
    from pathlib import Path

    from computer_use_replay.contracts import Fill
    from computer_use_replay.policy import Binding

    binding = Binding.load(Path("profiles/juniper_live.json"))
    if fault == "overlapping_scopes":
        data = binding.model_dump(mode="json")
        data["live_scopes"].append({**data["live_scopes"][0], "name": "lookup_alias"})
        binding = Binding.model_validate(data)
    async with live() as (execution, _):
        execution.policy.binding = binding
        surface = execution.surface
        await surface.navigate()
        snapshot = await surface.observe()
        field = next(c for c in snapshot.live_candidates if c.kind == "field")
        parameter = "member_id" if fault == "overlapping_scopes" else "nickname"
        code = "ambiguous_target" if fault == "overlapping_scopes" else "input_target_mismatch"
        with pytest.raises(Stop, match=code):
            await surface.perform(
                Fill(target=field.candidate_id, input=parameter), {parameter: "00123"}
            )
        assert (
            await surface.page.frame(name="workbench")
            .locator("input[name=member_key]")
            .input_value()
            == ""
        )
        assert not surface._live_pinned_handles


@pytest.mark.parametrize("attribute", ["formaction", "formmethod"])
async def test_empty_submit_overrides_use_browser_effective_destination(live, attribute):
    from pathlib import Path

    from computer_use_replay.contracts import Click, Condition, Fill
    from computer_use_replay.policy import Binding

    async with live() as (execution, _):
        execution.policy.binding = Binding.load(Path("profiles/juniper_live.json"))
        surface = execution.surface
        await surface.navigate()
        snapshot = await surface.observe()
        field = next(c for c in snapshot.live_candidates if c.kind == "field")
        await surface.perform(
            Fill(target=field.candidate_id, input="member_id"), {"member_id": "00123"}
        )
        snapshot = await surface.observe()
        button = next(c for c in snapshot.live_candidates if c.kind == "button")
        frame = surface.page.frame(name="workbench")
        if attribute == "formaction":
            # Empty formaction resolves to the document URL, not the form action.
            await frame.evaluate("history.replaceState(null, '', '/desk/ready')")
        await frame.locator("button").evaluate(
            "(el, attribute) => el.setAttribute(attribute, '')", attribute
        )
        with pytest.raises(Stop, match="action_policy"):
            await surface.perform(
                Click(target=button.candidate_id, after=Condition(target="results_screen")), {}
            )
        assert not (await surface.observe()).live_candidates
        assert not surface._live_pinned_handles
