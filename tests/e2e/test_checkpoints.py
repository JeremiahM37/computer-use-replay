"""Observed action receipts, output identity, counts, and numeric checkpoints."""

from pathlib import Path

import pytest
from fastapi.responses import HTMLResponse

from computer_use_replay.contracts import (
    Capability,
    Click,
    Condition,
    GoalRequest,
    Input,
    Output,
    Read,
    Target,
)
from computer_use_replay.discovery import discover, learn_click
from computer_use_replay.engine import Replay
from computer_use_replay.planner import Decision
from computer_use_replay.policy import Binding, Control, Stop


@pytest.mark.parametrize("kind", ["equals_input", "visible", "absent", "unchanged", "multiple"])
async def test_click_receipt_requires_newly_true_fact(live, kind):
    async with live() as (ex, _):
        await ex.surface.navigate()
        frame = ex.surface.page.frame(name="workbench")
        controls = dict(ex.policy.binding.controls)
        for key, selector in [("search", "#go"), ("balance", "#result")]:
            controls[key] = controls[key].model_copy(
                update={"target": Target(kind="css", name=selector, frames=("workbench",))}
            )
        ex.policy.binding = ex.policy.binding.model_copy(
            update={"controls": controls, "step_timeout": 2.0}
        )
        html = (
            '<span id="result"'
            + (" hidden" if kind in {"visible", "multiple"} else "")
            + ">00123</span>"
        )
        effect = "document.querySelector('#result').hidden=false"
        if kind == "equals_input":
            html = '<span id="result">old</span>'
            effect = "document.querySelector('#result').textContent='00123'"
        if kind == "absent":
            effect = "document.querySelector('#result').remove()"
        if kind == "unchanged":
            effect = ""
        await frame.set_content(
            html
            + '<button id="go" onclick="window.clicks=(window.clicks||0)+1;'
            + effect
            + '">Apply</button>'
        )
        eq = Condition(target="balance", kind="equals_input", input="member_id")
        checkpoint = Condition(target="balance", kind=kind) if kind in {"visible", "absent"} else eq
        candidates = (Condition(target="balance"), eq) if kind == "multiple" else (checkpoint,)
        step = Click(target="search", after=Condition(target="search"))
        if kind == "unchanged":
            with pytest.raises(Stop, match="checkpoint_failed"):
                await learn_click(
                    ex, step, {"member_id": "00123"}, 0, await ex.surface.observe(), candidates
                )
        else:
            learned = await learn_click(
                ex, step, {"member_id": "00123"}, 0, await ex.surface.observe(), candidates
            )
            assert learned.after == checkpoint
        assert await frame.evaluate("window.clicks") == 1


async def test_checkpoint_group_is_rechecked_after_output_read(live, capability):
    async with live() as (ex, _):
        ex.policy.binding = ex.policy.binding.model_copy(update={"step_timeout": 2.0})
        capability = capability.model_copy(update={"binding_sha256": ex.policy.binding.digest()})
        perform = ex.surface.perform

        async def change_identity_after_read(step, arguments):
            value = await perform(step, arguments)
            if isinstance(step, Read):
                identity = await ex.surface._unique("member_identity")
                await identity.evaluate("el => el.textContent = '00456'")
            return value

        ex.surface.perform = change_identity_after_read
        result = await Replay(ex).run(capability, {"member_id": "00123"})
        assert result.status == "failure", result
        assert result.failure.code == "checkpoint_failed"
        assert result.failure.target == "member_identity"
        assert "outputs" not in result.model_dump()


@pytest.mark.parametrize("initial", [1, 2])
async def test_discovered_count_receipt_and_unrequested_extra_row(live, tmp_path, initial):
    async with live() as (ex, app):

        @app.get("/records")
        async def records():
            return HTMLResponse(
                '<div id="rows">'
                + '<button class="record">Record</button>' * initial
                + "</div><button id=\"add\" onclick=\"document.querySelector('#rows').insertAdjacentHTML('beforeend','<button class=record>Record</button>');document.querySelector('output').textContent=document.querySelectorAll('.record').length\">Add record</button><output>Pending</output>"
            )

        count = Condition(target="records", kind="count_equals_input", input="record_count")
        binding = Binding(
            product="records",
            entry="/records",
            routes={"/records": ("GET",)},
            controls={
                "records": Control(
                    target=Target(kind="css", name=".record"),
                    operations=("click",),
                    description="Visible records",
                ),
                "add": Control(
                    target=Target(kind="css", name="#add"),
                    operations=("click",),
                    description="Add one record",
                ),
                "result": Control(
                    target=Target(kind="css", name="output"),
                    operations=("read",),
                    description="Displayed count",
                ),
            },
            states={},
            input_types={"record_count": Input(kind="integer", minimum=2, maximum=2)},
            invariants=(count,),
            step_timeout=2.0,
        )
        ex.policy.binding = binding
        request = GoalRequest(
            name="records",
            goal="Finish with exactly two records",
            inputs=binding.input_types,
            outputs={"count": Output(kind="text", source="result")},
            checkpoint=(count,),
        )
        if initial == 1:

            class Planner:
                mode = "test_fixture"
                model = "fixture"
                calls = 0

                async def decide(self, context):
                    choices = [
                        Decision(
                            op="click", target="add", after="records", reason="follow_navigation"
                        ),
                        Decision(
                            op="read", target="result", output="count", reason="extract_output"
                        ),
                    ]
                    decision = choices[self.calls]
                    self.calls += 1
                    return decision

            artifact, result = await discover(
                ex, Planner(), request, {"record_count": 2}, tmp_path / "count.json"
            )
            assert result.status == "success", result
            assert artifact.steps[0].after == count
            artifact = Capability.model_validate_json((tmp_path / "count.json").read_text())
        else:
            from computer_use_replay.contracts import Provenance

            artifact = Capability(
                name=request.name,
                product=binding.product,
                binding_sha256=binding.digest(),
                targets={k: v.target for k, v in binding.controls.items()},
                inputs=request.inputs,
                outputs=request.outputs,
                checkpoint=request.checkpoint,
                steps=(Click(target="add", after=count), Read(target="result", output="count")),
                provenance=Provenance(
                    mode="test_fixture", model="fixture", calls=0, run_id="extra-row"
                ),
            )
        result = await Replay(ex).run(artifact, {"record_count": 2})
        if initial == 1:
            assert result.status == "success", result
            assert result.outputs == {"count": "2"}
            with pytest.raises(Stop, match="ambiguous_target"):
                await ex.surface.perform(Click(target="records", after=count), {})
            with pytest.raises(Stop, match="ambiguous_target"):
                await ex.surface.condition(Condition(target="records"), {})
        else:
            assert result.status == "failure", result
            assert result.failure.code == "checkpoint_failed"
            assert result.failure.target == "records"
            assert result.failure.match_count == 3
        assert result.llm_calls == 0


async def test_zero_count_requires_a_resolvable_target(live):
    async with live() as (ex, _):
        await ex.surface.navigate()
        controls = dict(ex.policy.binding.controls)
        controls["balance"] = controls["balance"].model_copy(
            update={"target": Target(kind="css", name=".absent", frames=("workbench",))}
        )
        ex.policy.binding = ex.policy.binding.model_copy(update={"controls": controls})
        c = Condition(target="balance", kind="count_equals_input", input="count")
        assert await ex.surface.condition(c, {"count": 0})
        assert not await ex.surface.condition(c, {"count": False})
        assert not await ex.surface.condition(c, {"count": "0"})
        await ex.surface.page.locator("iframe").evaluate("el => el.remove()")
        assert not await ex.surface.condition(c, {"count": 0})


@pytest.mark.parametrize("initial_ready", [False, True])
async def test_output_phase_rejects_early_reads_and_later_actions(live, tmp_path, initial_ready):
    async with live() as (ex, app):

        @app.get("/stale")
        async def page():
            markup = "<button onclick=\"document.querySelector('output').textContent='00123'\">Select</button><output>Not selected</output>"
            if initial_ready:
                markup = markup.replace("textContent='00123'", "textContent='00999'").replace(
                    "<output>Not selected</output>", "<output>00123</output>"
                )
            return HTMLResponse(markup)

        checkpoint = Condition(target="result", kind="equals_input", input="member_id")
        binding = Binding(
            product="stale",
            entry="/stale",
            routes={"/stale": ("GET",)},
            controls={
                "choose": Control(
                    target=Target(kind="role", role="button", name="Select"),
                    operations=("click",),
                    description="Select requested record",
                ),
                "result": Control(
                    target=Target(kind="css", name="output"),
                    operations=("read",),
                    description="Selected record",
                ),
            },
            states={},
            input_types={"member_id": Input(kind="identifier", pattern="[0-9]{5}")},
            invariants=(checkpoint,),
        )
        ex.policy.binding = binding
        outputs = {"selected": Output(kind="text", source="result")}
        if initial_ready:
            outputs["second"] = Output(kind="text", source="result")
        request = GoalRequest(
            name="stale_probe",
            goal="Select and return requested record",
            inputs=binding.input_types,
            outputs=outputs,
            checkpoint=(checkpoint,),
        )

        class Planner:
            model = "fixture"
            mode = "test_fixture"
            calls = 0

            async def decide(self, context):
                steps = [
                    Decision(
                        op="read", target="result", output="selected", reason="extract_output"
                    ),
                    Decision(
                        op="click", target="choose", after="choose", reason="follow_navigation"
                    ),
                ]
                step = steps[self.calls]
                self.calls += 1
                return step

        artifact, result = await discover(
            ex, Planner(), request, {"member_id": "00123"}, tmp_path / "artifact.json"
        )
        assert artifact is None
        assert result.failure.code == ("output_order" if initial_ready else "checkpoint_failed")
        assert not (tmp_path / "artifact.json").exists()
        assert await ex.surface.page.locator("output").inner_text() == (
            "00123" if initial_ready else "Not selected"
        )


@pytest.mark.parametrize("quantity", [2, 7])
async def test_formatted_integer_discovery_and_replay(live, tmp_path: Path, quantity):
    async with live() as (ex, app):

        @app.get("/numeric")
        async def numeric():
            return HTMLResponse(
                """<label>Quantity<input oninput="window.fills=(window.fills||0)+1" onchange="this.value=Number(this.value).toFixed(3)"></label><button onclick="document.querySelector('output').textContent=document.querySelector('input').value">Calculate</button><output>Pending</output>"""
            )

        checkpoint = Condition(target="result", kind="equals_integer_input", input="quantity")
        binding = Binding(
            product="numeric",
            entry="/numeric",
            routes={"/numeric": ("GET",)},
            controls={
                "quantity": Control(
                    target=Target(kind="label", name="Quantity"),
                    operations=("fill",),
                    description="Requested count",
                    allowed_inputs=("quantity",),
                ),
                "calculate": Control(
                    target=Target(kind="role", role="button", name="Calculate"),
                    operations=("click",),
                    description="Calculate",
                ),
                "result": Control(
                    target=Target(kind="css", name="output"),
                    operations=("read",),
                    description="Calculated count",
                ),
            },
            states={},
            input_types={"quantity": Input(kind="integer", minimum=1, maximum=9)},
            invariants=(checkpoint,),
        )
        ex.policy.binding = binding
        request = GoalRequest(
            name="count",
            goal="Calculate the requested count",
            inputs=binding.input_types,
            outputs={"count": Output(kind="text", source="result")},
            checkpoint=(checkpoint,),
        )

        class Planner:
            mode = "test_fixture"
            model = "fixture"
            calls = 0

            async def decide(self, context):
                if self.calls:
                    assert "quantity" in context["verified_completed_fields"]
                    assert "fill" not in context["catalog"]["quantity"]["operations"]
                choices = [
                    Decision(
                        op="fill", target="quantity", input="quantity", reason="enter_parameter"
                    ),
                    Decision(
                        op="click", target="calculate", after="result", reason="follow_navigation"
                    ),
                    Decision(op="read", target="result", output="count", reason="extract_output"),
                ]
                result = choices[self.calls]
                self.calls += 1
                return result

        artifact, result = await discover(
            ex, Planner(), request, {"quantity": quantity}, tmp_path / "numeric.json"
        )
        assert result.status == "success", result
        assert result.outputs == {"count": f"{quantity}.000"}
        assert artifact.steps[1].after == checkpoint
        assert await ex.surface.page.evaluate("window.fills") == 1
        # Normal serialization and replay exercise the declared numeric condition.
        artifact = type(artifact).model_validate_json((tmp_path / "numeric.json").read_text())
        result = await Replay(ex).run(artifact, {"quantity": 9})
        assert result.status == "success", result
        assert result.outputs == {"count": "9.000"}
        assert result.llm_calls == 0
        assert await ex.surface.page.evaluate("window.fills") == 1
