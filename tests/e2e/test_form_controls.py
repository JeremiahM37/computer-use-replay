"""Verified field commits, native controls, and explicit paragraph entry."""

import json

import pytest
from fastapi.responses import HTMLResponse
from playwright.async_api import Locator
from playwright.async_api import TimeoutError as BrowserTimeout

from computer_use_replay.contracts import (
    Capability,
    Condition,
    Fill,
    Input,
    Output,
    Provenance,
    Read,
    Target,
)
from computer_use_replay.engine import Replay
from computer_use_replay.policy import Binding, Control, Stop


def form_contract(spec, target, *, fills=1):
    condition = Condition(target="field", kind="equals_input", input="value")
    binding = Binding(
        product="typed_form",
        entry="/typed",
        routes={"/typed": ("GET",)},
        controls={
            "field": Control(
                target=Target(kind="css", name=target),
                operations=("fill",),
                allowed_inputs=("value",),
                description="Reviewed field",
            ),
            "receipt": Control(
                target=Target(kind="css", name="output"),
                operations=("read",),
                description="Application receipt",
            ),
        },
        input_types={"value": spec},
        states={},
        invariants=(condition,),
        # Real browser actions must finish inside this budget on slow CI runners too.
        step_timeout=1.0,
    )
    artifact = Capability(
        name="enter_value",
        product=binding.product,
        binding_sha256=binding.digest(),
        targets={k: v.target for k, v in binding.controls.items()},
        inputs=binding.input_types,
        outputs={"receipt": Output(source="receipt", kind="text")},
        checkpoint=(condition,),
        provenance=Provenance(mode="test_fixture", model="fixture", calls=0, run_id="test"),
        steps=(Fill(target="field", input="value"),) * fills
        + (Read(target="receipt", output="receipt"),),
    )
    return binding, artifact


@pytest.mark.parametrize("initial", [False, True])
@pytest.mark.parametrize("desired", [False, True])
async def test_checkbox_assignment_is_idempotent_and_verified(live, initial, desired):
    async with live() as (ex, app):

        @app.get("/typed")
        async def form():
            return HTMLResponse(
                f'<input id="field" type="checkbox" {"checked" if initial else ""}>'
                f"<output>{'enabled' if initial else 'disabled'}</output>"
                "<script>window.changes=0;field.onchange=()=>{window.changes++;"
                'document.querySelector("output").textContent=field.checked?"enabled":"disabled";};</script>'
            )

        binding, artifact = form_contract(Input(kind="boolean"), "#field", fills=2)
        ex.policy.binding = binding
        result = await Replay(ex).run(artifact, {"value": desired})
        assert result.status == "success", result
        assert result.outputs == {"receipt": "enabled" if desired else "disabled"}
        assert await ex.surface.page.evaluate("window.changes") == int(initial != desired)
        assert await ex.surface.page.locator("#field").is_checked() is desired
        assert result.llm_calls == 0


@pytest.mark.parametrize("variant", ["text", "radio", "mixed"])
async def test_boolean_does_not_misinterpret_other_controls_or_mixed_state(live, variant):
    async with live() as (ex, app):

        @app.get("/typed")
        async def form():
            tag = "checkbox" if variant == "mixed" else variant
            script = "<script>field.indeterminate=true</script>" if variant == "mixed" else ""
            return HTMLResponse(
                f'<input id="field" type="{tag}"><output>unchanged</output>{script}'
            )

        binding, artifact = form_contract(Input(kind="boolean"), "#field")
        ex.policy.binding = binding
        result = await Replay(ex).run(artifact, {"value": True})
        assert result.status == "failure", result
        assert result.failure.code == "unsupported_control"
        assert not await ex.surface.condition(artifact.checkpoint[0], {"value": True})
        assert "outputs" not in result.model_dump()


@pytest.mark.parametrize("fault", ["timeout", "reverted"])
async def test_checkbox_uncertain_or_reverted_assignment_stops_without_repeating(
    live, monkeypatch, fault
):
    original = Locator.set_checked
    calls = 0

    async def disrupted(locator, value, **kwargs):
        nonlocal calls
        calls += 1
        await original(locator, value, **kwargs)
        if fault == "timeout":
            raise BrowserTimeout("accepted click then lost confirmation")
        await locator.evaluate("el=>el.checked=false")

    monkeypatch.setattr(Locator, "set_checked", disrupted)
    async with live() as (ex, app):

        @app.get("/typed")
        async def form():
            return HTMLResponse('<input id="field" type="checkbox"><output>result</output>')

        binding, artifact = form_contract(Input(kind="boolean"), "#field")
        ex.policy.binding = binding
        result = await Replay(ex).run(artifact, {"value": True})
        assert result.status == "failure", result
        assert result.failure.code == (
            "effect_uncertain" if fault == "timeout" else "fill_mismatch"
        )
        assert calls == 1
        assert "outputs" not in result.model_dump()
        events = [
            json.loads(x) for x in (ex.evidence.directory / "events.jsonl").read_text().splitlines()
        ]
        assert len([e for e in events if e["event"] == "action_started"]) == 1


@pytest.mark.parametrize("tag", ["textarea", "editable"])
@pytest.mark.parametrize("value", ["Single line", "First — line\nSecond line", "First\n\nThird"])
async def test_multiline_text_is_verified_and_returns_exact_visible_content(live, tag, value):
    async with live() as (ex, app):

        @app.get("/typed")
        async def form():
            field = (
                '<textarea id="field"></textarea>'
                if tag == "textarea"
                else '<div id="field" contenteditable="true"></div>'
            )
            return HTMLResponse(
                field + "<output></output><script>field.oninput=()=>{"
                'document.querySelector("output").textContent=field.value??field.innerText;};</script><style>output{white-space:pre-wrap}</style>'
            )

        binding, artifact = form_contract(Input(kind="multiline"), "#field")
        ex.policy.binding = binding
        result = await Replay(ex).run(artifact, {"value": value})
        assert result.status == "success", (
            result,
            await ex.surface.page.locator("#field").inner_text(),
            await ex.surface.page.locator("#field").inner_html(),
        )
        assert result.outputs["receipt"] == value


@pytest.mark.parametrize(
    "kind,value,changed",
    [
        ("boolean", False, True),
        ("multiline", "PRIVATE first\nPRIVATE second", "Changed first\nChanged second"),
    ],
)
async def test_discovery_verifies_typed_assignments_and_replays_changed_inputs(
    live, tmp_path, kind, value, changed
):
    from computer_use_replay.contracts import GoalRequest
    from computer_use_replay.discovery import discover
    from tests.support.planner import ScriptedPlanner, decisions

    field = (
        '<input id="field" type="checkbox">'
        if kind == "boolean"
        else '<div id="field" contenteditable="true"></div>'
    )
    html = (
        field
        + '<output>disabled</output><script>field.oninput=()=>{document.querySelector("output").textContent='
        '(field.type==="checkbox"?(field.checked?"enabled":"disabled"):field.innerText);};</script><style>output{white-space:pre-wrap}</style>'
    )
    binding, reference = form_contract(Input(kind=kind), "#field")
    task = GoalRequest(
        name="enter_value",
        goal="Assign value and return the application receipt",
        inputs=binding.input_types,
        outputs=reference.outputs,
        checkpoint=reference.checkpoint,
    )

    class Planner(ScriptedPlanner):
        async def decide(self, context):
            assert "PRIVATE first" not in json.dumps(context)
            if not context["history"]:
                assert context["verified_field_assignments"]["field"] == []
                assert not context["checkpoint_ready"]
            else:
                assert context["current_input_matches"]["field"] == ["value"]
                assert context["verified_field_assignments"]["field"] == ["value"]
                assert "fill" not in context["catalog"]["field"]["operations"]
            return await super().decide(context)

    async with live() as (ex, app):

        @app.get("/typed")
        async def form():
            return HTMLResponse(html)

        ex.policy.binding = binding
        artifact, result = await discover(
            ex, Planner(decisions(reference)), task, {"value": value}, tmp_path / "typed.json"
        )
        assert result.status == "success", result
        assert artifact.provenance.mode == "test_fixture"
    async with live() as (ex, app):

        @app.get("/typed")
        async def form_again():
            return HTMLResponse(html)

        ex.policy.binding = binding
        result = await Replay(ex).run(artifact, {"value": changed})
        assert result.status == "success", result
        assert result.llm_calls == 0
        assert result.outputs["receipt"] == ("enabled" if kind == "boolean" else changed)


@pytest.mark.parametrize("value", ["One line", "First\nSecond", "First\n\nThird"])
async def test_reviewed_paragraph_editor_preserves_logical_lines(live, value):
    async with live() as (ex, app):

        @app.get("/typed")
        async def form():
            return HTMLResponse(
                '<div id="field" contenteditable="true"><p><br></p></div><output>unused</output>'
                '<script>field.onkeydown=e=>{if(e.key==="Enter"){e.preventDefault();document.execCommand("insertParagraph");}};</script>'
            )

        binding, artifact = form_contract(Input(kind="multiline"), "#field")
        binding.controls["field"] = binding.controls["field"].model_copy(
            update={"text_mode": "paragraphs", "operations": ("fill", "read")}
        )
        artifact = artifact.model_copy(
            update={
                "binding_sha256": binding.digest(),
                "outputs": {"receipt": Output(source="field", kind="text")},
                "steps": (
                    Fill(target="field", input="value"),
                    Read(target="field", output="receipt"),
                ),
            }
        )
        ex.policy.binding = binding
        result = await Replay(ex).run(artifact, {"value": value})
        assert result.status == "success", result
        assert result.outputs["receipt"] == value
        assert await ex.surface.page.locator("#field > p").count() == len(value.split("\n"))


@pytest.mark.parametrize(
    "html",
    [
        '<div id="field"><p>Text</p></div>',
        '<div id="field" contenteditable="true">Unwrapped</div>',
        '<div id="field" contenteditable="true"><h2>Heading</h2></div>',
        '<div id="field" contenteditable="true"><!-- comment --><p>Text</p></div>',
    ],
)
async def test_paragraph_mode_does_not_guess_an_unreviewed_structure(live, html):
    from computer_use_replay.policy import Stop

    async with live() as (ex, app):

        @app.get("/typed")
        async def form():
            return HTMLResponse(html)

        binding, artifact = form_contract(Input(kind="multiline"), "#field")
        binding.controls["field"] = binding.controls["field"].model_copy(
            update={"text_mode": "paragraphs"}
        )
        ex.policy.binding = binding
        await ex.surface.navigate()
        with pytest.raises(Stop, match="unsupported_control"):
            await ex.surface.condition(artifact.checkpoint[0], {"value": "Text"})


async def test_paragraph_read_preserves_empty_blocks_and_excludes_hidden_text(live):
    async with live() as (ex, app):

        @app.get("/typed")
        async def form():
            return HTMLResponse(
                '<div id="field" contenteditable="true">\n<p><strong>First</strong></p><p><br></p><p>Third<span hidden>PRIVATE HIDDEN</span></p>\n</div>'
            )

        binding, _ = form_contract(Input(kind="multiline"), "#field")
        ex.policy.binding = binding
        await ex.surface.navigate()
        assert (
            await ex.surface._text(await ex.surface._unique("field"), paragraphs=True)
            == "First\n\nThird"
        )


async def test_multiline_checkpoint_does_not_trim_a_new_blank_paragraph(live):
    async with live() as (ex, app):

        @app.get("/typed")
        async def form():
            return HTMLResponse(
                '<div id="field" contenteditable="true"><p>First</p><p>Second</p><p><br></p></div>'
            )

        binding, artifact = form_contract(Input(kind="multiline"), "#field")
        binding.controls["field"] = binding.controls["field"].model_copy(
            update={"text_mode": "paragraphs"}
        )
        ex.policy.binding = binding
        await ex.surface.navigate()
        assert not await ex.surface.condition(artifact.checkpoint[0], {"value": "First\nSecond"})


@pytest.mark.parametrize("tag", ["input", "select"])
@pytest.mark.parametrize("commit_key,uncertain", [(None, False), ("Tab", False), ("Tab", True)])
async def test_field_commit_is_opt_in_and_precedes_output_verification(
    live, tag, commit_key, uncertain, monkeypatch
):
    if uncertain:
        press = Locator.press

        async def uncertain_press(locator, key, **kwargs):
            await press(locator, key, **kwargs)
            raise BrowserTimeout("accepted keyboard event but confirmation timed out")

        monkeypatch.setattr(Locator, "press", uncertain_press)
    async with live() as (ex, app):

        @app.get("/commit")
        async def form():
            field = (
                '<input id="date">'
                if tag == "input"
                else '<select id="date"><option></option><option>10-01-2026</option></select>'
            )
            return HTMLResponse(
                field
                + """<button>Next</button><output></output><script>
                window.commits=0;
                document.querySelector('#date').addEventListener('keydown', e => {
                    if(e.key==='Tab') {
                        window.commits++;
                        document.querySelector('output').textContent=e.target.value;
                    }
                });
                </script>"""
            )

        receipt = Condition(target="committed", kind="equals_input", input="date")
        binding = Binding(
            product="date_form",
            entry="/commit",
            routes={"/commit": ("GET",)},
            controls={
                "date": Control(
                    target=Target(kind="css", name="#date"),
                    operations=("fill",),
                    description="Date entry",
                    allowed_inputs=("date",),
                    commit_key=commit_key,
                ),
                "committed": Control(
                    target=Target(kind="css", name="output"),
                    operations=("read",),
                    description="Application-committed date",
                ),
            },
            states={},
            input_types={"date": Input(kind="text")},
            invariants=(receipt,),
            # Generous on purpose: the uncertain case is injected after a real key press.
            step_timeout=1.0,
        )
        ex.policy.binding = binding
        artifact = Capability(
            name="enter_date",
            product=binding.product,
            binding_sha256=binding.digest(),
            targets={k: v.target for k, v in binding.controls.items()},
            inputs=binding.input_types,
            outputs={"date": Output(source="committed", kind="text")},
            checkpoint=(receipt,),
            provenance=Provenance(mode="test_fixture", model="fixture", calls=0, run_id="test"),
            steps=(Fill(target="date", input="date"), Read(target="committed", output="date")),
        )
        result = await Replay(ex).run(artifact, {"date": "10-01-2026"})
        assert await ex.surface.page.evaluate("window.commits") == int(commit_key == "Tab")
        if uncertain:
            assert result.status == "failure", result
            assert result.failure.code == "effect_uncertain"
            events = [
                json.loads(x)
                for x in (ex.evidence.directory / "events.jsonl").read_text().splitlines()
            ]
            assert len([e for e in events if e["event"] == "action_started"]) == 1
            assert "outputs" not in result.model_dump()
        elif commit_key:
            assert result.status == "success", result
            assert result.outputs["date"] == "10-01-2026"
        else:
            assert result.status == "failure", result
            assert result.failure.code == "checkpoint_failed"
            assert "outputs" not in result.model_dump()


@pytest.mark.parametrize(
    "markup,expected",
    [
        ('<input id="value" value="SYNTHETIC-CUSTOMER-001">', "SYNTHETIC-CUSTOMER-001"),
        ('<textarea id="value">Synthetic delivery note</textarea>', "Synthetic delivery note"),
        (
            '<select id="value"><option value="sales">Sales</option>'
            '<option value="maint" selected>Maintenance</option></select>',
            "Maintenance",
        ),
        ('<div id="value">$ 4,470.00</div>', "$ 4,470.00"),
    ],
)
async def test_displayed_form_value_used_for_output_and_checkpoint(live, markup, expected):
    async with live() as (ex, _):
        await ex.surface.navigate()
        frame = ex.surface.page.frame(name="workbench")
        await frame.set_content(markup)
        controls = dict(ex.policy.binding.controls)
        controls["balance"] = controls["balance"].model_copy(
            update={"target": Target(kind="css", name="#value", frames=("workbench",))}
        )
        ex.policy.binding = ex.policy.binding.model_copy(update={"controls": controls})
        assert await ex.surface.perform(Read(target="balance", output="value"), {}) == expected
        condition = Condition(target="balance", kind="equals_input", input="expected")
        assert await ex.surface.condition(condition, {"expected": expected})
        assert not await ex.surface.condition(condition, {"expected": "WRONG RECORD"})
        # Observation/evidence must not acquire a new channel for customer values.
        assert expected not in (await ex.surface.observe()).model_dump_json()


async def test_reviewed_selector_still_requires_one_visible_match(live):
    async with live() as (ex, _):
        await ex.surface.navigate()
        frame = ex.surface.page.frame(name="workbench")
        controls = dict(ex.policy.binding.controls)
        controls["balance"] = controls["balance"].model_copy(
            update={
                "target": Target(kind="css", name='[data-fieldname="total"]', frames=("workbench",))
            }
        )
        ex.policy.binding = ex.policy.binding.model_copy(update={"controls": controls})
        await frame.set_content(
            '<input data-fieldname="total" value="17">'
            '<input hidden data-fieldname="total" value="wrong">'
        )
        assert await ex.surface.perform(Read(target="balance", output="value"), {}) == "17"
        await frame.locator("[hidden]").evaluate("el => el.hidden = false")
        with pytest.raises(Stop, match="ambiguous_target"):
            await ex.surface.perform(Read(target="balance", output="value"), {})
        await frame.set_content("<p>Record removed</p>")
        with pytest.raises(Stop, match="target_missing"):
            await ex.surface.perform(Read(target="balance", output="value"), {})


@pytest.mark.parametrize(
    "markup,code",
    [
        (
            '<select id="value"><option>Sales</option><option value="m">Maintenance</option></select>',
            None,
        ),
        ('<select id="value"><option>Sales</option></select>', "selection_unavailable"),
        (
            '<select id="value"><option>Maintenance</option><option>Maintenance</option></select>',
            "selection_unavailable",
        ),
        (
            '<select id="value" multiple><option>Maintenance</option></select>',
            "selection_unavailable",
        ),
        ('<select id="value" disabled><option>Maintenance</option></select>', "control_not_ready"),
        ('<select id="value"><option disabled>Maintenance</option></select>', "control_not_ready"),
        (
            '<select id="value"><optgroup disabled label="Unavailable"><option>Maintenance</option></optgroup></select>',
            "control_not_ready",
        ),
        (
            '<select id="value" onchange="this.selectedIndex=0"><option>Sales</option><option>Maintenance</option></select>',
            "fill_mismatch",
        ),
    ],
)
async def test_native_dropdown_assignment_is_verified(live, markup, code):
    from computer_use_replay.contracts import Fill

    async with live() as (ex, _):
        await ex.surface.navigate()
        await ex.surface.page.frame(name="workbench").set_content(markup)
        controls = dict(ex.policy.binding.controls)
        controls["member_input"] = controls["member_input"].model_copy(
            update={"target": Target(kind="css", name="#value", frames=("workbench",))}
        )
        ex.policy.binding = ex.policy.binding.model_copy(update={"controls": controls})
        step = Fill(target="member_input", input="member_id")
        if code:
            with pytest.raises(Stop, match=code):
                await ex.surface.perform(step, {"member_id": "Maintenance"})
        else:
            await ex.surface.perform(step, {"member_id": "Maintenance"})
            loc = await ex.surface._unique("member_input")
            assert await loc.input_value() == "m"
            assert await ex.surface.condition(
                Condition(target="member_input", kind="equals_input", input="member_id"),
                {"member_id": "Maintenance"},
            )
