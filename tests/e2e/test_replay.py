"""Model-free replay, capability reuse, and reviewed deployment contracts."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from computer_use_replay.browser import BrowserSurface
from computer_use_replay.contracts import (
    Capability,
    Click,
    Condition,
    Fill,
    GoalRequest,
    Output,
    Read,
)
from computer_use_replay.control import Handoff, Ownership
from computer_use_replay.demo import serve_demo
from computer_use_replay.discovery import discover
from computer_use_replay.engine import Execution, Replay
from computer_use_replay.evidence import Evidence
from computer_use_replay.policy import Binding, Policy, Stop
from tests.fixtures.legacy_vendor import serve_vendor
from tests.support.planner import ScriptedPlanner, decisions


async def test_real_browser_changed_input_and_money(live, capability):
    async with live() as (ex, app):
        result = await Replay(ex).run(capability, {"member_id": "00456"})
        assert result.status == "success", result
        assert result.outputs == {"available_balance": {"amount": "8902.10", "currency": "USD"}}
        assert result.llm_calls == 0
        assert app.state.finalizations == 0
        assert "00123" not in capability.model_dump_json()


async def test_unchanged_tenant_replay_emits_no_presentation_drift_event(live, capability):
    async with live() as (ex, _):
        result = await Replay(ex).run(capability, {"member_id": "00456"})
        assert result.status == "success", result
        raw = (ex.evidence.directory / "events.jsonl").read_text()
        assert '"presentation_drift"' not in raw


async def test_committed_read_savings_replays_on_tenant_b_with_one_drift_event(tmp_path):
    # The exact committed submission artifact, not a hand-authored test fixture.
    savings_path = Path("capabilities/read_savings.json")
    savings = Capability.model_validate_json(
        savings_path.read_text()  # noqa: ASYNC240 — tiny one-shot repo-relative read, like elsewhere in this module
    )
    binding = Binding.load(Path("profiles/juniper.json"))
    overlay = binding.overlay(Path("profiles/tenant_b.json"))
    async with serve_demo("tenant_b") as (origin, app):
        evidence = Evidence(tmp_path)
        owner = Ownership(evidence)
        policy = Policy(overlay, origin)
        async with BrowserSurface(policy, owner, evidence) as surface:
            result = await Replay(Execution(surface, policy, evidence, Handoff(owner))).run(
                savings, {"member_id": "00456"}
            )
            assert result.status == "success", result
            assert result.outputs == {"available_balance": {"amount": "8902.10", "currency": "USD"}}
            assert result.llm_calls == 0 and app.state.finalizations == 0
    rows = [json.loads(x) for x in (evidence.directory / "events.jsonl").read_text().splitlines()]
    drift = [row for row in rows if row["event"] == "presentation_drift"]
    assert len(drift) == 1, drift
    # tenant_b.json relabels three controls and renames the shared "workbench" frame,
    # so every recorded target key -- not just the three relabeled ones -- differs.
    assert sorted(drift[0]["targets"]) == sorted(savings.targets)
    assert drift[0]["count"] == len(drift[0]["targets"])


@pytest.mark.parametrize(
    "member,code",
    [("99999", "member_not_found"), ("00888", "permission_denied"), ("00000", "validation_error")],
)
async def test_business_outcomes(live, capability, member, code):
    async with live() as (ex, _):
        result = await Replay(ex).run(capability, {"member_id": member})
        assert result.status == "business_outcome", result
        assert result.code == code
        assert result.llm_calls == 0


@pytest.mark.parametrize("scenario", ["notice", "slow"])
async def test_recoverable_states(live, capability, scenario):
    async with live(scenario) as (ex, _):
        result = await Replay(ex).run(capability, {"member_id": "00123"})
        assert result.status == "success", result
        events = (ex.evidence.directory / "events.jsonl").read_text()
        if scenario == "notice":
            assert '"event": "recovery"' in events


@pytest.mark.parametrize(
    "scenario,code",
    [
        ("failed", "service_unavailable"),
        ("duplicate", "ambiguous_target"),
        ("wrong_member", "checkpoint_failed"),
        ("malformed_money", "output_type_mismatch"),
        ("expired", "operator_unavailable"),
        ("dialog", "operator_unavailable"),
        ("native_dialog", "native_dialog"),
        ("external_request", "network_policy"),
        ("external_redirect", "network_policy"),
    ],
)
async def test_explicit_failures(live, capability, scenario, code):
    async with live(scenario) as (ex, app):
        result = await Replay(ex).run(capability, {"member_id": "00123"})
        assert result.status == "failure", result
        assert result.failure.code == code, result
        assert result.failure.evidence
        assert result.failure.intervention_id
        assert app.state.finalizations == 0


async def test_live_session_handoff_and_navigation_capture(live, capability):
    async with live("expired") as (ex, _):
        page = ex.surface.page
        context = ex.surface.context

        async def operator(owner, request, validate):
            lease = await owner.claim(request)
            with pytest.raises(Stop, match="ownership_conflict"):
                await ex.surface.perform(capability.steps[0], {"member_id": "00456"})
            with pytest.raises(Stop, match="resume_condition_unmet"):
                await owner.resume(lease, validate)
            await (
                page.frame(name="workbench")
                .get_by_role("button", name="Renew session", exact=True)
                .click()
            )
            await (
                page.frame(name="workbench")
                .get_by_role("heading", name="Account directory", exact=True)
                .wait_for()
            )
            await owner.resume(lease, validate)

        ex.handoff.operator = operator
        result = await Replay(ex).run(capability, {"member_id": "00123"})
        assert result.status == "success", result
        assert ex.surface.page is page and ex.surface.context is context
        rows = [
            json.loads(x) for x in (ex.evidence.directory / "events.jsonl").read_text().splitlines()
        ]
        assert any(
            x["event"] == "operator_action" and x["action_kind"] == "navigation" for x in rows
        )
        assert any(x["event"] == "operator_action" and x["action_kind"] == "click" for x in rows)
        assert len({x["session_id"] for x in rows if "session_id" in x}) == 1
        assert ex.handoff.ownership.owner == "automation"


async def test_evidence_has_no_values_in_any_file(live, capability):
    async with live() as (ex, _):
        result = await Replay(ex).run(capability, {"member_id": "00123"})
        assert result.status == "success", result
        for path in ex.evidence.directory.rglob("*"):
            if path.is_file():
                text = path.read_text()
                for secret in [
                    "00123",
                    "1204.57",
                    "$1,204.57",
                    "SYNTHETIC PERSON",
                    "PRIVATE-NOTE",
                    "member_key",
                    "workstation=",
                ]:
                    assert secret not in text, (path, secret)


@pytest.mark.parametrize("task_name", ["read_savings", "prepare_subaccount"])
async def test_two_goals_replay_unchanged_on_second_tenant(
    live, capability, tmp_path, binding, task_name
):
    request = GoalRequest.load(Path(f"requests/{task_name}.json"))
    args = {"member_id": "00123"}
    recording = capability
    if task_name == "prepare_subaccount":
        args["nickname"] = "Rainy day"
        recording = capability.model_copy(
            update={
                "steps": capability.steps[:4]
                + (
                    Click(target="subaccount", after=Condition(target="subaccount_screen")),
                    Fill(target="nickname_input", input="nickname"),
                    Click(
                        target="review_subaccount", after=Condition(target="confirmation_screen")
                    ),
                    Read(target="review_status", output="review_status"),
                )
            }
        )
    path = tmp_path / "learned.json"
    async with live() as (ex, app):
        learned, result = await discover(
            ex, ScriptedPlanner(decisions(recording)), request, args, path
        )
        assert result.status == "success", result
        assert learned.name == task_name
        assert learned.inputs == request.inputs and learned.outputs == request.outputs
        assert app.state.finalizations == 0
    original = path.read_bytes()
    overlay = binding.overlay(Path("profiles/tenant_b.json"))
    assert overlay.digest() == binding.digest()
    async with serve_demo("tenant_b") as (origin, app):
        evidence = Evidence(tmp_path / "replays")
        owner = Ownership(evidence)
        policy = Policy(overlay, origin)
        async with BrowserSurface(policy, owner, evidence) as surface:
            args["member_id"] = "00456"
            if "nickname" in args:
                args["nickname"] = "New buffer"
            result = await Replay(Execution(surface, policy, evidence, Handoff(owner))).run(
                learned, args
            )
            assert result.status == "success", result
            assert result.llm_calls == 0
            assert app.state.finalizations == 0
            assert path.read_bytes() == original
            if task_name == "read_savings":
                assert result.outputs["available_balance"]["amount"] == "8902.10"
            else:
                assert result.outputs == {"review_status": "Ready for confirmation"}
                assert (
                    await surface.page.frame(name="operations")
                    .get_by_role("button", name="Create sub-account", exact=True)
                    .count()
                    == 1
                )


async def test_missing_label_has_actionable_private_diagnostics(live, capability):
    async with live() as (ex, _):
        original = ex.surface.navigate

        async def changed():
            await original()
            await (
                ex.surface.page.frame(name="workbench")
                .get_by_role("button", name="Find member", exact=True)
                .evaluate("el=>el.textContent='Search member'")
            )

        ex.surface.navigate = changed
        result = await Replay(ex).run(capability, {"member_id": "00123"})
        # Live DOM anomaly, not a reviewed presentation change: the recorded hint
        # still equals the current binding's target, so hint_differs is False even
        # though the control itself has zero matches at the deadline.
        assert result.failure.code == "target_drift"
        assert result.failure.hint_differs is False
        assert result.failure.step == 1
        assert result.failure.locator.frames == ("workbench",)
        assert result.failure.locator.name == "Find member"
        assert result.failure.match_count == 0
        assert result.failure.target == "search"
        assert (
            (ex.evidence.directory / result.failure.screenshot).read_bytes().startswith(b"\x89PNG")
        )
        assert "00123" not in result.model_dump_json()


async def test_masked_screenshot_conceals_values_and_restores_live_session(live):
    async with live() as (ex, _):
        await ex.surface.navigate()
        field = ex.surface.page.frame(name="workbench").get_by_label("Member identifier")
        await field.fill("SECRET-A")
        name = await ex.surface.failure_screenshot()
        before = (ex.evidence.directory / name).read_bytes()
        await field.fill("SECRET-B")
        await ex.surface.failure_screenshot()
        assert (ex.evidence.directory / name).read_bytes() == before
        assert await field.input_value() == "SECRET-B"
        assert await field.evaluate("el=>getComputedStyle(el).color") != "rgba(0, 0, 0, 0)"
        with patch.object(
            ex.surface.page, "screenshot", new=AsyncMock(side_effect=RuntimeError("renderer"))
        ):
            result = await ex.fail(Stop("surface_error"), 0)
            assert result.failure.screenshot is None
            assert result.failure.evidence


async def test_masked_screenshot_preserves_structure_unlike_a_blank_page(live):
    # No Pillow dependency to count non-background pixels: PNG byte size/hash is the
    # comparison the design calls for when a heavier image library isn't warranted.
    async with live() as (ex, _):
        await ex.surface.navigate()
        structured_name = await ex.surface.failure_screenshot()
        structured = (ex.evidence.directory / structured_name).read_bytes()
        await ex.surface.page.set_content("<html><body></body></html>")
        blank_name = await ex.surface.failure_screenshot()
        blank = (ex.evidence.directory / blank_name).read_bytes()
        assert structured != blank
        # Headings, table rows and a button each paint a distinct neutral block; a
        # blank page has none, so the structured capture encodes visibly more.
        assert len(structured) > len(blank) * 1.1


@pytest.mark.parametrize(
    "value", [{}, [], {"member_id": 123}, {"member_id": "00123", "extra": True}]
)
async def test_invalid_input_does_not_touch_surface_or_pause(live, capability, value):
    async with live() as (ex, app):
        with patch.object(
            ex.surface,
            "observe",
            new=AsyncMock(side_effect=AssertionError("invalid input observed UI")),
        ):
            result = await Replay(ex).run(capability, value)
        assert result.failure.code == "invalid_input"
        assert result.failure.intervention_id is None
        assert result.failure.evidence is None
        assert ex.handoff.ownership.owner == "automation"
        assert app.state.sessions == {}
        assert "intervention_requested" not in (ex.evidence.directory / "events.jsonl").read_text()


@pytest.mark.parametrize(
    "field,value",
    [("risk", "blocked"), ("operations", []), ("description", "different product semantics")],
)
def test_presentation_cannot_weaken_product_contract(binding, capability, field, value):
    raw = binding.model_dump(mode="json")
    raw["controls"]["search"][field] = value
    changed = Binding.model_validate(raw)
    with pytest.raises(Stop, match="binding_mismatch"):
        Policy(changed, "http://localhost").check_artifact(capability)


def test_overlay_rejects_policy_fields_and_wrong_vocabulary(binding, tmp_path):
    path = tmp_path / "overlay.json"
    for raw in [
        {
            "product": binding.product,
            "tenant": "b",
            "targets": {"search": binding.controls["search"].target.model_dump(mode="json")},
            "risk": "reversible",
        },
        {
            "product": "other",
            "tenant": "b",
            "targets": {"search": binding.controls["search"].target.model_dump(mode="json")},
        },
        {
            "product": binding.product,
            "tenant": "b",
            "targets": {"unknown": binding.controls["search"].target.model_dump(mode="json")},
        },
    ]:
        path.write_text(json.dumps(raw))
        with pytest.raises(ValueError):
            binding.overlay(path)


def test_identifier_pattern_is_product_configuration(binding):
    from computer_use_replay.contracts import Input

    with pytest.raises(ValidationError, match="configured pattern"):
        Input(kind="identifier")
    spec = Input(kind="identifier", pattern="M-[A-Z]{2}[0-9]{3}", max_length=8)
    assert spec.validate_value("M-AB007") == "M-AB007"
    for value in ["00123", "M-AB007\n", "M-AB0007"]:
        with pytest.raises(ValueError):
            spec.validate_value(value)
    request = GoalRequest.load(Path("requests/read_savings.json"))
    raw = binding.model_dump(mode="json")
    raw["input_types"]["member_id"] = spec.model_dump(mode="json")
    policy = Policy(Binding.model_validate(raw), "http://localhost")
    policy.check_request(request.model_copy(update={"inputs": {"member_id": spec}}))
    with pytest.raises(Stop, match="contract_mismatch"):
        policy.check_request(request)


async def test_failed_observation_does_not_claim_a_current_match_count(live):
    async with live() as (ex, _):
        await ex.surface.navigate()
        await ex.surface.observe()
        ex.current_target = "search"
        with patch.object(ex.surface, "observe", new=AsyncMock(side_effect=RuntimeError("closed"))):
            result = await ex.fail(Stop("session_closed"), 0)
        assert result.failure.locator.name == "Find member"
        assert result.failure.match_count is None


async def test_invalid_submit_is_withheld_and_rechecked_at_dispatch(live):
    from computer_use_replay.planner import action_tools

    async with live() as (ex, app):
        await ex.surface.navigate()
        snapshot = await ex.surface.observe()
        search = next(n for n in snapshot.controls if n.target == "search")
        assert search.ready is False
        context = {
            "observation": snapshot.model_dump(mode="json"),
            "catalog": {
                k: {"operations": v.operations, "risk": v.risk}
                for k, v in ex.policy.binding.controls.items()
            },
            "inputs": {"member_id": {}},
            "outputs": {},
        }
        offered = action_tools(context)
        assert not any(t["function"]["name"] == "click_control" for t in offered)
        with pytest.raises(Stop, match="control_not_ready"):
            await ex.surface.perform(
                Click(target="search", after=Condition(target="results_screen")), {}
            )
        assert all("member" not in session for session in app.state.sessions.values())
        await ex.surface.perform(
            Fill(target="member_input", input="member_id"), {"member_id": "00123"}
        )
        snapshot = await ex.surface.observe()
        assert next(n for n in snapshot.controls if n.target == "search").ready is True
        await ex.surface.perform(
            Click(target="search", after=Condition(target="results_screen")), {}
        )
        await ex.settle(Condition(target="results_screen"))
        assert await ex.surface.condition(Condition(target="results_screen"), {})


async def test_readiness_does_not_wait_for_a_control_removed_by_navigation(live):
    import asyncio

    async with live() as (ex, _):
        await ex.surface.navigate()
        locator = ex.surface.page.frame(name="workbench").get_by_role(
            "button", name="Missing submit"
        )
        assert await asyncio.wait_for(ex.surface._ready(locator), 1) is False


@pytest.mark.parametrize("layout", ["framed", "inline"])
@pytest.mark.parametrize("task", ["read_savings", "prepare_subaccount"])
@pytest.mark.parametrize("member", ["00123", "00456", "00999", "00888", "00000"])
async def test_identical_artifact_on_independent_vendor(
    binding, capability, subaccount_capability, tmp_path, layout, task, member
):
    artifact = capability if task == "read_savings" else subaccount_capability
    # Serialize/load before running: no live object or discovery session reuse.
    saved = artifact.model_dump_json()
    artifact = Capability.model_validate_json(saved)
    binding = binding.overlay(Path(f"profiles/legacy_{layout}.json"))
    async with serve_vendor(layout) as (origin, app):
        evidence = Evidence(tmp_path)
        owner = Ownership(evidence)
        policy = Policy(binding, origin)
        async with BrowserSurface(policy, owner, evidence) as surface:
            arguments = {"member_id": member}
            if task == "prepare_subaccount":
                arguments["nickname"] = "Fresh requested alias"
            result = await Replay(Execution(surface, policy, evidence, Handoff(owner))).run(
                artifact, arguments
            )
            if member in {"00999", "00888", "00000"}:
                assert result.status == "business_outcome", result
                assert (
                    result.code
                    == {
                        "00999": "member_not_found",
                        "00888": "permission_denied",
                        "00000": "validation_error",
                    }[member]
                )
            else:
                assert result.status == "success", result
                if task == "read_savings":
                    assert (
                        result.outputs["available_balance"]["amount"]
                        == {"00123": "2701.32", "00456": "5306.49"}[member]
                    )
                else:
                    assert result.outputs == {"review_status": "Ready for confirmation"}
            assert result.llm_calls == 0 and app.state.finalizations == 0
            assert artifact.model_dump_json() == saved
            assert "finalize" not in app.state.posts


@pytest.mark.parametrize("layout", ["framed", "inline"])
@pytest.mark.parametrize("fault", ["nickname", "status", "duplicate"])
async def test_independent_vendor_review_corruption(
    binding, subaccount_capability, tmp_path, layout, fault
):
    binding = binding.overlay(Path(f"profiles/legacy_{layout}.json"))
    async with serve_vendor(layout, fault) as (origin, app):
        evidence = Evidence(tmp_path)
        owner = Ownership(evidence)
        policy = Policy(binding, origin)
        async with BrowserSurface(policy, owner, evidence) as surface:
            result = await Replay(Execution(surface, policy, evidence, Handoff(owner))).run(
                subaccount_capability, {"member_id": "00456", "nickname": "New alias"}
            )
            assert result.status == "failure", result
            assert (
                result.failure.code
                == {
                    "nickname": "checkpoint_failed",
                    "status": "output_type_mismatch",
                    "duplicate": "ambiguous_target",
                }[fault]
            )
            assert app.state.finalizations == 0


@pytest.mark.parametrize("tenant", ["normal", "tenant_b"])
@pytest.mark.parametrize(
    "fault", ["none", "nickname", "status", "missing", "hidden", "duplicate", "stale"]
)
async def test_review_must_match_invocation(
    binding, subaccount_capability, tmp_path, tenant, fault
):
    if tenant == "tenant_b":
        binding = binding.overlay(Path("profiles/tenant_b.json"))
    async with serve_demo(tenant) as (origin, app):
        route = next(r for r in app.routes if r.path == "/desk/subaccount/review")
        original = route.app

        async def corrupt(scope, receive, send):
            async def response(message):
                if message["type"] == "http.response.start":
                    message = dict(
                        message,
                        headers=[(k, v) for k, v in message["headers"] if k != b"content-length"],
                    )
                if message["type"] == "http.response.body":
                    body = message.get("body", b"")
                    row = b"<tr><th>Nickname</th><td>Requested buffer</td></tr>"
                    replacements = {
                        "nickname": row.replace(b"Requested buffer", b"Wrong buffer"),
                        "missing": b"",
                        "hidden": row.replace(b"<tr>", b'<tr style="display:none">'),
                        "duplicate": row + row,
                        "stale": row.replace(b"Requested buffer", b"Previously entered nickname"),
                    }
                    if fault in replacements:
                        assert row in body
                        body = body.replace(row, replacements[fault])
                    if fault == "status":
                        body = body.replace(b"Ready for confirmation", b"Rejected by supervisor")
                    message = dict(message, body=body)
                await send(message)

            await original(scope, receive, response)

        route.app = corrupt
        evidence = Evidence(tmp_path)
        owner = Ownership(evidence)
        policy = Policy(binding, origin)
        async with BrowserSurface(policy, owner, evidence) as surface:
            result = await Replay(Execution(surface, policy, evidence, Handoff(owner))).run(
                subaccount_capability, {"member_id": "00456", "nickname": "Requested buffer"}
            )
            if fault == "none":
                assert result.status == "success"
            else:
                assert result.status == "failure"
                assert result.failure.code == (
                    "output_type_mismatch"
                    if fault == "status"
                    else "ambiguous_target"
                    if fault == "duplicate"
                    else "checkpoint_failed"
                )
            assert result.llm_calls == 0 and app.state.finalizations == 0


@pytest.mark.parametrize("fault", ["omit_nickname", "weaken_status"])
def test_artifact_cannot_remove_review_guarantees(binding, subaccount_capability, fault):
    if fault == "omit_nickname":
        changed = subaccount_capability.model_copy(
            update={
                "checkpoint": tuple(
                    c for c in subaccount_capability.checkpoint if c.target != "review_nickname"
                )
            }
        )
    else:
        changed = subaccount_capability.model_copy(
            update={"outputs": {"review_status": Output(kind="text", source="review_status")}}
        )
    with pytest.raises(Stop, match="checkpoint_mismatch|output_contract_mismatch"):
        Policy(binding, "http://localhost").check_artifact(changed)


@pytest.mark.parametrize(
    "kind,values", [("money", ("Ready",)), ("text", ("",)), ("text", ("Ready", "Ready"))]
)
def test_output_enumeration_shape(kind, values):
    with pytest.raises(ValueError):
        Output(kind=kind, allowed_values=values)
