"""Asynchronous lookup receipts, exact matches, and reusable field context."""

import html
import json

import pytest
from fastapi.responses import HTMLResponse

from computer_use_replay.browser import BrowserSurface
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
    Target,
    TargetScope,
)
from computer_use_replay.control import Ownership
from computer_use_replay.discovery import discover
from computer_use_replay.engine import Replay
from computer_use_replay.evidence import Evidence
from computer_use_replay.planner import Decision, action_tools
from computer_use_replay.policy import Binding, Control, Policy, Stop
from tests.support.planner import TASK, ScriptedPlanner, decisions


@pytest.mark.parametrize("value", ["Alpha", "Beta"])
@pytest.mark.parametrize("mode", ["discovery", "replay"])
@pytest.mark.parametrize("outcome", ["found", "missing", "duplicate"])
async def test_delayed_lookup_receipt(live, tmp_path, value, mode, outcome):
    async with live() as (ex, app):

        @app.get("/lookup")
        async def lookup():
            return HTMLResponse(
                """<input oninput="window.fills=(window.fills||0)+1;const v=this.value;setTimeout(()=>{document.querySelector('#choices').replaceChildren();if(MODE==='missing')return;for(let i=0;i<(MODE==='duplicate'?2:1);i++){const b=document.createElement('button');b.textContent=v;b.onclick=()=>{document.querySelector('output').textContent=v};document.querySelector('#choices').append(b)}},150)"><div id="choices"></div><output>Not selected</output>""".replace(
                    "MODE", html.escape(json.dumps(outcome), quote=True)
                )
            )

        checkpoint = Condition(target="result", kind="equals_input", input="query")
        b = Binding(
            product="lookup",
            entry="/lookup",
            routes={"/lookup": ("GET",)},
            controls={
                "field": Control(
                    target=Target(kind="css", name="input"),
                    description="Search",
                    operations=("fill",),
                    allowed_inputs=("query",),
                    after_fill=Condition(target="choice"),
                ),
                "choice": Control(
                    target=Target(kind="css", name="#choices button"),
                    description="Matching result",
                    operations=("click",),
                    match_input="query",
                ),
                "result": Control(
                    target=Target(kind="css", name="output"),
                    description="Selected result",
                    operations=("read",),
                ),
            },
            states={},
            input_types={"query": Input(kind="text")},
            invariants=(checkpoint,),
            step_timeout=2.0,  # real actions must land inside this on slow CI runners
        )
        ex.policy.binding = b
        request = GoalRequest(
            name="lookup",
            goal="Select requested result",
            inputs=b.input_types,
            outputs={"value": Output(kind="text", source="result")},
            checkpoint=(checkpoint,),
        )
        steps = (
            Fill(target="field", input="query"),
            Click(target="choice", after=checkpoint),
            Read(target="result", output="value"),
        )
        if mode == "discovery":

            class Planner:
                mode = "test_fixture"
                model = "fixture"
                calls = 0

                async def decide(self, context):
                    s = steps[self.calls]
                    self.calls += 1
                    if s.op == "click":
                        assert any(
                            n["target"] == "choice" and n["count"] == 1
                            for n in context["observation"]["controls"]
                        )
                    raw = s.model_dump()
                    if s.op == "click":
                        raw["after"] = "choice"
                    return Decision(
                        **raw,
                        reason={
                            "fill": "enter_parameter",
                            "click": "follow_navigation",
                            "read": "extract_output",
                        }[s.op],
                    )

            artifact, result = await discover(
                ex, Planner(), request, {"query": value}, tmp_path / "learned.json"
            )
            if outcome == "found":
                assert result.status == "success", result
                assert artifact.targets["choice"] == b.controls["choice"].target
                assert value not in (tmp_path / "learned.json").read_text()
        else:
            artifact = Capability(
                name=request.name,
                product=b.product,
                binding_sha256=b.digest(),
                targets={k: c.target for k, c in b.controls.items()},
                inputs=request.inputs,
                outputs=request.outputs,
                checkpoint=request.checkpoint,
                steps=steps,
                provenance=Provenance(mode="test_fixture", model="fixture", calls=0, run_id="test"),
            )
            result = await Replay(ex).run(artifact, {"query": value})
        assert await ex.surface.page.evaluate("window.fills") == 1
        if outcome == "found":
            assert result.status == "success", result
            assert result.outputs == {"value": value}
        else:
            assert result.failure.code == (
                "ambiguous_target" if outcome == "duplicate" else "target_drift"
            ), result
            if outcome == "missing":
                # Discovery never records an artifact to compare against; replay's
                # hand-built artifact here reuses the binding's own targets unchanged.
                assert result.failure.hint_differs == (False if mode == "replay" else None)
            assert result.failure.target == "choice"
            assert result.failure.match_count == (0 if outcome == "missing" else 2)
            assert await ex.surface.page.locator("output").inner_text() == "Not selected"


@pytest.mark.parametrize("value", ["CP-CUSTOMER-001", "[a-z].*", 'Quoted "customer"'])
async def test_exact_parameter_match_and_private_observation(tmp_path, value):
    binding = Binding(
        product="test",
        entry="/",
        routes={"/": ("GET",)},
        controls={
            "choice": Control(
                target=Target(kind="css", name=".option"),
                match_input="customer",
                description="Exact customer",
                operations=("click",),
            )
        },
        states={},
        input_types={"customer": Input(kind="text")},
        invariants=(Condition(target="choice"),),
    )
    evidence = Evidence(tmp_path)
    async with BrowserSurface(
        Policy(binding, "http://localhost"), Ownership(evidence), evidence
    ) as surface:
        await surface.page.set_content(
            '<button class="option" onclick="this.dataset.selected=1">'
            + html.escape(value)
            + '</button><button class="option">Different customer</button>'
        )
        assert await surface.count("choice") == 0
        surface.bind_arguments({"customer": value})
        assert await surface.count("choice") == 1
        assert json.dumps(value)[1:-1] not in (await surface.observe()).model_dump_json()
        await surface.perform(
            Click(target="choice", after=Condition(target="choice")), {"customer": value}
        )
        assert await surface.page.locator('[data-selected="1"]').inner_text() == value
        await surface.page.locator('[data-selected="1"]').evaluate(
            "el=>el.after(el.cloneNode(true))"
        )
        with pytest.raises(Stop, match="ambiguous_target"):
            await surface._unique("choice")


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("customer", ["CP-CUSTOMER-001", "CP-CUSTOMER-002"])
@pytest.mark.parametrize("mode", ["replay", "discovery"])
async def test_runtime_binds_input_to_result_selection(live, customer, mode, tmp_path, scoped):
    from fastapi.responses import HTMLResponse

    from computer_use_replay.contracts import Capability, Provenance, Read
    from computer_use_replay.engine import Replay

    async with live() as (ex, app):

        @app.get("/picker")
        async def picker():
            return HTMLResponse("""<div class="choice"><button class="option" onclick="document.querySelector('output').textContent=this.textContent;document.querySelector('h2').hidden=false">CP-CUSTOMER-001</button></div>
            <div class="choice"><button class="option" onclick="document.querySelector('output').textContent=this.textContent;document.querySelector('h2').hidden=false">CP-CUSTOMER-002</button></div><output>Not selected</output><h2 hidden>Customer selected</h2>""")

        condition = Condition(target="result", kind="equals_input", input="customer")
        binding = Binding(
            product="picker",
            entry="/picker",
            routes={"/picker": ("GET",)},
            controls={
                "choice": Control(
                    target=Target(
                        kind="css",
                        name=".option",
                        scope=TargetScope(container=".choice", anchor=".option")
                        if scoped
                        else None,
                    ),
                    match_input="customer",
                    description="Exact requested customer",
                    operations=("click",),
                ),
                "selected_screen": Control(
                    target=Target(kind="role", role="heading", name="Customer selected"),
                    description="Selection finished",
                ),
                "result": Control(
                    target=Target(kind="css", name="output"),
                    description="Selected customer",
                    operations=("read",),
                ),
            },
            states={},
            input_types={"customer": Input(kind="text")},
            invariants=(condition,),
        )
        from computer_use_replay.contracts import Output

        artifact = Capability(
            name="select_customer",
            product=binding.product,
            binding_sha256=binding.digest(),
            targets={k: v.target for k, v in binding.controls.items()},
            inputs=binding.input_types,
            outputs={"selected": Output(kind="text", source="result")},
            checkpoint=(condition,),
            steps=(
                Click(target="choice", after=condition),
                Read(target="result", output="selected"),
            ),
            provenance=Provenance(
                mode="test_fixture", model="fixture", calls=0, run_id="parameter-test"
            ),
        )
        ex.policy.binding = binding
        if mode == "discovery":
            from computer_use_replay.contracts import GoalRequest
            from computer_use_replay.discovery import discover
            from computer_use_replay.planner import Decision

            class FixturePlanner:
                calls = 0
                mode = "test_fixture"
                model = "fixture"

                async def decide(self, context):
                    assert customer not in json.dumps(context)
                    decisions = [
                        Decision(
                            op="click", target="choice", after="choice", reason="follow_navigation"
                        ),
                        Decision(
                            op="read", target="result", output="selected", reason="extract_output"
                        ),
                    ]
                    decision = decisions[self.calls]
                    self.calls += 1
                    return decision

            request = GoalRequest(
                name=artifact.name,
                goal="Select the requested customer and return its identifier",
                inputs=artifact.inputs,
                outputs=artifact.outputs,
                checkpoint=artifact.checkpoint,
            )
            path = tmp_path / "learned.json"
            learned, result = await discover(
                ex, FixturePlanner(), request, {"customer": customer}, path
            )
            assert learned is not None
            assert customer not in path.read_text()
        else:
            result = await Replay(ex).run(artifact, {"customer": customer})
        assert result.status == "success"
        assert result.outputs == {"selected": customer}
        assert result.llm_calls == (2 if mode == "discovery" else 0)
        assert customer not in artifact.model_dump_json()


@pytest.mark.parametrize("alternate", ["00456", "00123"])
async def test_reusable_field_keeps_alternatives_and_reports_current_matches(
    live, capability, tmp_path, alternate
):
    async with live() as (ex, _):
        raw = ex.policy.binding.model_dump(mode="json")
        raw["input_types"]["alternate_id"] = raw["input_types"]["member_id"]
        raw["controls"]["member_input"]["allowed_inputs"] = ["member_id", "alternate_id"]
        raw["controls"]["persistent_editor"] = {
            "description": "Reusable editor that remains visible after navigation",
            "target": {"kind": "css", "name": "#persistent-editor"},
            "operations": ["fill"],
            "allowed_inputs": ["member_id", "alternate_id"],
        }
        ex.policy.binding = Binding.model_validate(raw)
        navigate = ex.surface.navigate

        async def with_persistent_editor():
            await navigate()
            await ex.surface.page.evaluate(
                "document.body.insertAdjacentHTML('beforeend', '<input id=persistent-editor>')"
            )

        ex.surface.navigate = with_persistent_editor
        request = GoalRequest.model_validate(
            {
                **TASK.model_dump(),
                "inputs": {**TASK.inputs, "alternate_id": TASK.inputs["member_id"]},
            }
        )
        steps = list(decisions(capability))
        steps.insert(0, steps[0].model_copy(update={"input": "alternate_id"}))

        class Planner(ScriptedPlanner):
            calls = 0

            async def decide(self, context):
                if self.calls <= 2:
                    field = context["catalog"]["member_input"]
                    assert list(field["allowed_inputs"]) == ["member_id", "alternate_id"]
                    assert "fill" in field["operations"]
                    assert "member_input" not in context["verified_completed_fields"]
                if self.calls == 1:
                    expected = (
                        ["member_id", "alternate_id"] if alternate == "00123" else ["alternate_id"]
                    )
                    assert context["current_input_matches"]["member_input"] == expected
                    assert context["verified_field_assignments"]["member_input"] == ["alternate_id"]
                if self.calls == 2:
                    expected = (
                        ["member_id", "alternate_id"] if alternate == "00123" else ["member_id"]
                    )
                    assert context["current_input_matches"]["member_input"] == expected
                    assert context["verified_field_assignments"]["member_input"] == expected
                if all(c["satisfied"] for c in context["checkpoint_status"]):
                    assert context["checkpoint_ready"]
                    assert context["current_input_matches"]["persistent_editor"] == []
                    assert any(
                        t["function"]["name"] == "read_control" for t in action_tools(context)
                    )
                assert [c["condition"] for c in context["checkpoint_status"]] == context[
                    "checkpoint"
                ]
                assert all(type(c["satisfied"]) is bool for c in context["checkpoint_status"])
                assert "00123" not in json.dumps(context)
                assert "00456" not in json.dumps(context)
                self.calls += 1
                return await super().decide(context)

        artifact, result = await discover(
            ex,
            Planner(steps),
            request,
            {"member_id": "00123", "alternate_id": alternate},
            tmp_path / "reusable.json",
        )
        assert result.status == "success", result
        assert isinstance(artifact.steps[0], Fill)
        assert artifact.steps[0].input == "alternate_id"
        binding = ex.policy.binding
    async with live() as (ex, _):
        ex.policy.binding = binding
        result = await Replay(ex).run(artifact, {"member_id": "00456", "alternate_id": "00123"})
        assert result.status == "success", result
        assert result.outputs["available_balance"]["amount"] == "8902.10"
