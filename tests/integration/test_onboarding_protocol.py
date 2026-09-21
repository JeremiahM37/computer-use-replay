"""End-to-end `propose()`: transport, retries and malformed-response protocol.

Uses the same httpx.MockTransport injection ModelPlanner tests use in
tests/integration/test_providers.py -- one bounded model interaction, the same
strict single-tool-call rules, no real network.
"""

import json
from pathlib import Path

import httpx
import pytest

from computer_use_replay.contracts import GoalRequest
from computer_use_replay.evidence import Evidence
from computer_use_replay.onboarding import propose
from computer_use_replay.policy import Policy, Stop
from computer_use_replay.providers import ENDPOINTS, ProviderConfig


def config(**kwargs):
    return ProviderConfig("ollama", "fixture-model", ENDPOINTS["ollama"], **kwargs)


VALID_ARGS = {
    "name": "read_savings_auto",
    "inputs": ["member_id"],
    "outputs": [{"name": "available_balance", "source": "balance", "kind": "money"}],
    "success_screen": "savings_screen",
}


def ollama_response(name="propose_contract", arguments=None):
    return {
        "done": True,
        "done_reason": "stop",
        "message": {
            "tool_calls": [{"function": {"name": name, "arguments": arguments or VALID_ARGS}}]
        },
        "prompt_eval_count": 200,
        "eval_count": 40,
    }


async def test_valid_proposal_end_to_end(binding, tmp_path):
    def handler(request):
        body = json.loads(request.content)
        assert request.url.path == "/api/chat"
        assert body["tools"][0]["function"]["name"] == "propose_contract"
        catalog = json.loads(body["messages"][-1]["content"])["catalog"]
        assert "routes" not in catalog and "risk" not in json.dumps(catalog)
        return httpx.Response(200, json=ollama_response())

    evidence = Evidence(tmp_path)
    request, calls = await propose(
        config(),
        evidence,
        binding,
        "Look up a member and read their available savings balance.",
        transport=httpx.MockTransport(handler),
    )
    assert calls == 1
    assert request.name == "read_savings_auto"
    assert request.review.status == "draft"
    assert request.review.proposed_by == "ollama/fixture-model"
    Policy(binding, "http://localhost").check_request(request)
    rows = [
        json.loads(line) for line in (evidence.directory / "events.jsonl").read_text().splitlines()
    ]
    assert [r["event"] for r in rows] == ["model_response", "contract_proposed"]
    assert rows[1]["capability"] == "read_savings_auto" and rows[1]["llm_calls"] == 1


async def test_caller_supplied_name_overrides_the_models_choice(binding, tmp_path):
    request, _ = await propose(
        config(),
        evidence := Evidence(tmp_path),
        binding,
        "Look up a member and read their available savings balance.",
        name="pinned_name",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=ollama_response())),
    )
    assert request.name == "pinned_name"
    assert evidence.directory.exists()


async def test_prose_only_response_is_rejected(binding, tmp_path):
    def handler(_):
        return httpx.Response(200, json={"done": True, "done_reason": "stop", "message": {}})

    with pytest.raises(Stop, match="invalid_model_response"):
        await propose(
            config(),
            Evidence(tmp_path),
            binding,
            "Read the balance.",
            transport=httpx.MockTransport(handler),
        )


async def test_multiple_tool_calls_rejected(binding, tmp_path):
    def handler(_):
        return httpx.Response(
            200,
            json={
                "done": True,
                "done_reason": "stop",
                "message": {
                    "tool_calls": [
                        {"function": {"name": "propose_contract", "arguments": VALID_ARGS}},
                        {"function": {"name": "propose_contract", "arguments": VALID_ARGS}},
                    ]
                },
                "prompt_eval_count": 1,
                "eval_count": 1,
            },
        )

    with pytest.raises(Stop, match="invalid_model_response"):
        await propose(
            config(),
            Evidence(tmp_path),
            binding,
            "Read the balance.",
            transport=httpx.MockTransport(handler),
        )


async def test_offered_tool_arguments_reject_anything_not_enumerated(binding, tmp_path):
    def handler(_):
        return httpx.Response(200, json=ollama_response(arguments={**VALID_ARGS, "extra": 1}))

    with pytest.raises(Stop, match="invalid_model_response"):
        await propose(
            config(),
            Evidence(tmp_path),
            binding,
            "Read the balance.",
            transport=httpx.MockTransport(handler),
        )


async def test_oversized_response_is_rejected(binding, tmp_path):
    from computer_use_replay.planner import ModelPlanner

    def handler(_):
        return httpx.Response(200, content=b"{" + b" " * (ModelPlanner.MAX_RESPONSE_BYTES + 1))

    with pytest.raises(Stop, match="model_response_too_large"):
        await propose(
            config(),
            Evidence(tmp_path),
            binding,
            "Read the balance.",
            transport=httpx.MockTransport(handler),
        )


async def test_connection_failure_reports_model_unavailable(binding, tmp_path):
    def handler(_):
        raise httpx.ConnectError("PRIVATE transport error")

    with pytest.raises(Stop, match="model_unavailable"):
        await propose(
            config(retries=0),
            Evidence(tmp_path),
            binding,
            "Read the balance.",
            transport=httpx.MockTransport(handler),
        )


async def test_permanent_http_error_status_reports_model_unavailable(binding, tmp_path):
    # A non-retryable, non-auth status (e.g. 400) raises via response.raise_for_status()
    # inside ModelPlanner._request, not the retried ConnectError path above.
    def handler(_):
        return httpx.Response(400, text="PRIVATE-provider-error")

    with pytest.raises(Stop, match="model_unavailable"):
        await propose(
            config(retries=0),
            Evidence(tmp_path),
            binding,
            "Read the balance.",
            transport=httpx.MockTransport(handler),
        )


async def test_transient_failure_retries_then_succeeds(binding, tmp_path, monkeypatch):
    async def sleep(_):
        return None

    monkeypatch.setattr("computer_use_replay.planner.asyncio.sleep", sleep)
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) < 2:
            return httpx.Response(503, text="PRIVATE-error")
        return httpx.Response(200, json=ollama_response())

    request, calls = await propose(
        config(),
        Evidence(tmp_path),
        binding,
        "Read the balance.",
        transport=httpx.MockTransport(handler),
    )
    assert calls == 2 and request.name == "read_savings_auto"


async def test_policy_rejection_is_reported_as_proposal_invalid(binding, tmp_path, monkeypatch):
    class RejectingPolicy:
        def __init__(self, *args, **kwargs):
            pass

        def check_request(self, request):
            raise Stop("checkpoint_mismatch")

    monkeypatch.setattr("computer_use_replay.onboarding.Policy", RejectingPolicy)
    with pytest.raises(Stop, match="proposal_invalid"):
        await propose(
            config(),
            Evidence(tmp_path),
            binding,
            "Read the balance.",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=ollama_response())),
        )


async def test_proposal_time_out_is_reported(binding, tmp_path):
    def handler(_):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(Stop, match="model_timeout"):
        await propose(
            config(),
            Evidence(tmp_path),
            binding,
            "Read the balance.",
            transport=httpx.MockTransport(handler),
        )


def test_the_two_hand_written_requests_are_not_drafts():
    for name in ["read_savings", "prepare_subaccount"]:
        request = GoalRequest.load(Path(f"requests/{name}.json"))
        assert request.review is None
