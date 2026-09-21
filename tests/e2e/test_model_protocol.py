"""Real HTTP and browser integration with explicitly scripted model responses."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from computer_use_replay.contracts import Capability, GoalRequest
from computer_use_replay.discovery import discover
from computer_use_replay.engine import Replay
from computer_use_replay.planner import ModelPlanner
from computer_use_replay.providers import ENDPOINTS, ProviderConfig
from tests.provider_samples import FixtureTransport, response_for, wire_server

TASK = GoalRequest.load(Path("requests/read_savings.json"))


@pytest.mark.parametrize("provider", ENDPOINTS)
async def test_native_provider_http_discovery_and_changed_input_replay(provider, live, tmp_path):
    with wire_server(provider) as (origin, requests):
        async with live("notice") as (ex, _):
            config = ProviderConfig(
                provider, "explicit-wire-fixture", ENDPOINTS[provider], api_key="fixture-credential"
            )
            planner = ModelPlanner(config, ex.evidence, transport=FixtureTransport(origin))
            planner.mode = "test_fixture"
            path = tmp_path / "capability.json"
            artifact, result = await asyncio.wait_for(
                discover(ex, planner, TASK, {"member_id": "00123"}, path), 15
            )
            assert result.status == "success", result
            assert result.outputs["available_balance"]["amount"] == "1204.57"
            assert artifact.provenance.mode == "test_fixture"
            assert planner.calls == 7 and len(artifact.steps) == 6
            text = (ex.evidence.directory / "events.jsonl").read_text()
            rows = [json.loads(line) for line in text.splitlines()]
            started = [r["target"] for r in rows if r["event"] == "action_started"]
            assert started == [
                "member_input",
                "search",
                "open_member",
                "accounts",
                "dismiss_notice",
                "savings",
                "balance",
            ]
            assert len([r for r in rows if r["event"] == "model_retry"]) == 1
            assert len([r for r in rows if r["event"] == "completion_verified"]) == 1
            assert requests[1] == requests[2]
            for private in ["00123", "1204.57", "PRIVATE", "fixture-credential"]:
                assert private not in text + path.read_text() + json.dumps(requests)
        # Different server session, scenario and input; the artifact comes from disk.
        async with live("slow") as (ex, _):
            await ex.surface.page.set_viewport_size({"width": 440, "height": 900})
            saved = Capability.model_validate_json(path.read_text())
            result = await Replay(ex).run(saved, {"member_id": "00456"})
            assert result.status == "success", result
            assert result.outputs["available_balance"]["amount"] == "8902.10"
            assert result.llm_calls == 0 and len(requests) == 7


@pytest.mark.parametrize("status", [200, 500, "mismatch"])
async def test_rejected_prediction_has_no_browser_effect_before_valid_replacement(
    status, live, capability, tmp_path
):
    requests = 0
    async with live() as (ex, _):
        task = TASK
        arguments = {"member_id": "00123"}
        if status == "mismatch":
            binding = ex.policy.binding
            spec = binding.input_types["member_id"]
            binding.input_types["other_id"] = spec
            binding.controls["unused_input"] = binding.controls["member_input"].model_copy(
                update={"allowed_inputs": ("other_id",)}
            )
            task = TASK.model_copy(update={"inputs": {**TASK.inputs, "other_id": spec}})
            arguments["other_id"] = "00456"

        def handler(request):
            nonlocal requests
            requests += 1
            context = json.loads(json.loads(request.content)["messages"][-1]["content"])
            if requests == 1:
                if status == "mismatch":
                    return httpx.Response(
                        200,
                        json=response_for(
                            "ollama",
                            "fill_control",
                            {"target": "member_input", "parameter": "other_id"},
                        ),
                    )
                return httpx.Response(
                    status, json=response_for("ollama", "click_control", {"target": "finalize"})
                )
            step = capability.steps[len(context["history"])]
            args = {"target": step.target}
            if step.op == "fill":
                args["parameter"] = step.input
            if step.op == "read":
                args["output"] = step.output
            return httpx.Response(200, json=response_for("ollama", step.op + "_control", args))

        planner = ModelPlanner(
            ProviderConfig("ollama", "fixture", "http://model.test", decision_retries=1),
            ex.evidence,
            transport=httpx.MockTransport(handler),
        )
        planner.mode = "test_fixture"
        artifact, result = await discover(ex, planner, task, arguments, tmp_path / "learned.json")
        assert result.status == "success", result
        assert len(artifact.steps) == len(capability.steps)
        assert planner.calls == len(capability.steps) + 1
        events = [
            json.loads(x) for x in (ex.evidence.directory / "events.jsonl").read_text().splitlines()
        ]
        actions = [e for e in events if e["event"] == "action_started"]
        assert [e["target"] for e in actions] == [s.target for s in capability.steps]
        retry = next(e for e in events if e["event"] == "model_retry")
        assert retry["sequence"] < actions[0]["sequence"]
        assert not any(e["event"] == "intervention_requested" for e in events)
