"""Provider protocols, private model evidence, and bounded decision retries."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from computer_use_replay.contracts import GoalRequest
from computer_use_replay.evidence import Evidence
from computer_use_replay.planner import ModelPlanner
from computer_use_replay.policy import Stop
from computer_use_replay.providers import ENDPOINTS, KEY_ENV, ProviderConfig, decode, strict_json
from tests.provider_samples import response_for

TASK = GoalRequest.load(Path("requests/read_savings.json"))


@pytest.fixture
def context(binding):
    return {
        "inputs": {k: v.model_dump() for k, v in binding.input_types.items()},
        "outputs": {k: v.model_dump() for k, v in TASK.outputs.items()},
        "observation": {"controls": [{"target": "search", "count": 1}]},
        "catalog": {
            k: {"operations": v.operations, "risk": v.risk} for k, v in binding.controls.items()
        },
    }


def config(provider="openai", **kwargs):
    return ProviderConfig(
        provider, "fixture-model", ENDPOINTS[provider], api_key="PRIVATE-credential", **kwargs
    )


@pytest.mark.parametrize("provider", ENDPOINTS)
async def test_provider_native_contract_and_private_evidence(provider, context, tmp_path):
    def handler(request):
        body = json.loads(request.content)
        assert request.method == "POST"
        assert "PRIVATE-credential" not in str(request.url) + request.content.decode()
        if provider == "openai":
            assert request.url.path == "/v1/responses"
            assert body["store"] is False and body["parallel_tool_calls"] is False
            assert body["max_output_tokens"] == 2048
            assert all(t["strict"] for t in body["tools"])
            assert all("required" in t["parameters"] for t in body["tools"])
            assert body["instructions"] and body["input"]
        elif provider == "ollama":
            assert request.url.path == "/api/chat"
            assert "authorization" not in request.headers
            assert body["think"] is False and body["options"]["num_ctx"] == 8192
        if provider == "openai":
            assert request.headers["Authorization"] == "Bearer PRIVATE-credential"
        return httpx.Response(
            200,
            json=response_for(
                provider,
                "click_control",
                {"target": "search"},
            ),
        )

    evidence = Evidence(tmp_path)
    planner = ModelPlanner(config(provider), evidence, transport=httpx.MockTransport(handler))
    assert (await planner.decide(context)).target == "search"
    assert planner.calls == 1
    text = (evidence.directory / "events.jsonl").read_text()
    assert "PRIVATE-" not in text
    row = json.loads(text)
    assert row["provider"] == provider and row["prompt_tokens"] == 120
    assert row["output_tokens"] == 20 and len(row["response_sha256"]) == 64


@pytest.mark.parametrize("provider", ENDPOINTS)
@pytest.mark.parametrize(
    "name,args",
    [
        ("execute_shell", {}),
        ("click_control", {"target": "finalize"}),
        ("click_control", {"target": "search", "extra": "x"}),
        ("fill_control", {"target": "member_input", "parameter": "member_id"}),
        ("finish", {"secret": "PRIVATE-argument"}),
    ],
)
async def test_every_provider_cannot_escape_offered_tools(provider, name, args, context, tmp_path):
    planner = ModelPlanner(
        config(provider),
        Evidence(tmp_path),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=response_for(provider, name, args))
        ),
    )
    with pytest.raises(Stop, match="invalid_model_response"):
        await planner.decide(context)
    assert planner.calls == 1  # Invalid suggestions are never retried.


@pytest.mark.parametrize("provider", ENDPOINTS)
@pytest.mark.parametrize("payload", [None, [], {}, {"message": None}, "PRIVATE-response"])
async def test_malformed_envelopes_are_sanitized(provider, payload, context, tmp_path):
    evidence = Evidence(tmp_path)
    planner = ModelPlanner(
        config(provider),
        evidence,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
    )
    with pytest.raises(Stop, match="invalid_model_response"):
        await planner.decide(context)
    assert planner.calls == 1
    assert not (evidence.directory / "events.jsonl").exists()


@pytest.mark.parametrize("raw", ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}', "garbage"])
def test_json_ambiguity_is_rejected(raw):
    with pytest.raises(ValueError):
        strict_json(raw)


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "unknown"},
        {"model": "bad model"},
        {"model": ""},
        {"endpoint": "ftp://localhost"},
        {"endpoint": "http:///nohost"},
        {"endpoint": "http://user:password@localhost"},
        {"endpoint": "http://:pass@localhost"},
        {"endpoint": "http://localhost?api_key=PRIVATE"},
        {"endpoint": "http://localhost#x"},
        {"endpoint": "http://localhost:0"},
        {"endpoint": "http://local host"},
        {"endpoint": "http://localhost/%61"},
        {"endpoint": "http://localhost:99999"},
        {"endpoint": "http://[bad"},
        {"api_key": ""},
        {"api_key": "PRIVATE bad"},
        {"api_key": "PRIVATE-☺"},
        {"timeout": float("nan")},
        {"timeout": 0},
        {"timeout": 301},
        {"retries": -1},
        {"retries": 4},
        {"retries": True},
        {"max_tokens": 127},
        {"max_tokens": 16385},
        {"max_tokens": True},
    ],
)
def test_invalid_configuration_fails_before_any_request(changes):
    with pytest.raises(ValueError):
        replace(config(), **changes)


@pytest.mark.parametrize("provider", ["openai"])
def test_cloud_keys_cannot_be_redirected_to_arbitrary_endpoint(provider):
    with pytest.raises(ValueError, match="official endpoint"):
        replace(config(provider), endpoint="https://collector.invalid/v1")


def test_env_resolution_keeps_providers_separate(monkeypatch):
    for key in [*KEY_ENV.values(), "OLLAMA_URL", "OLLAMA_MODEL"]:
        monkeypatch.delenv(key, raising=False)
    assert ProviderConfig.from_env().model == "qwen3.6:35b-a3b"
    assert ProviderConfig.from_env().api_key == ""
    monkeypatch.setenv("OLLAMA_URL", "http://model.internal:11434")
    monkeypatch.setenv("OLLAMA_MODEL", "local-model:tag")
    assert ProviderConfig.from_env().endpoint == "http://model.internal:11434"
    assert ProviderConfig.from_env().model == "local-model:tag"
    for provider, key in KEY_ENV.items():
        monkeypatch.setenv(key, "PRIVATE-key-" + provider)
        chosen = ProviderConfig.from_env(provider, "chosen/model")
        assert chosen.endpoint == ENDPOINTS[provider]
        assert chosen.api_key == "PRIVATE-key-" + provider
        assert "PRIVATE" not in repr(chosen)
    with pytest.raises(ValueError, match="explicit model"):
        ProviderConfig.from_env("openai")
    with pytest.raises(ValueError, match="unknown provider"):
        ProviderConfig.from_env("nope", "model")


def test_missing_usage_is_supported():
    data = response_for("openai")
    del data["usage"]
    assert decode("openai", data)[1] == (None, None)


@pytest.mark.parametrize(
    "status,code",
    [
        (400, "model_unavailable"),
        (401, "model_authentication_failed"),
        (403, "model_authentication_failed"),
        (404, "model_unavailable"),
        (302, "model_unavailable"),
    ],
)
async def test_permanent_failures_and_redirects_never_retry(status, code, context, tmp_path):
    urls = []

    def handler(request):
        urls.append(str(request.url))
        return httpx.Response(
            status, headers={"Location": "https://collector.invalid"}, text="PRIVATE-provider-error"
        )

    planner = ModelPlanner(config(), Evidence(tmp_path), transport=httpx.MockTransport(handler))
    with pytest.raises(Stop, match=code):
        await planner.decide(context)
    assert urls == [ENDPOINTS["openai"] + "/responses"]
    assert planner.calls == 1


@pytest.mark.parametrize("provider", ENDPOINTS)
@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 529, "connect"])
async def test_transient_failures_retry_only_model_request(
    provider, status, context, tmp_path, monkeypatch
):
    context = {**context, "checkpoint_ready": True, "collected_outputs": list(context["outputs"])}
    delays, requests = [], []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("computer_use_replay.planner.asyncio.sleep", sleep)

    def handler(request):
        requests.append(request.content)
        if len(requests) < 3:
            if status == "connect":
                raise httpx.ConnectError("PRIVATE transport error")
            return httpx.Response(status, headers={"Retry-After": "1.5"}, text="PRIVATE-error")
        return httpx.Response(200, json=response_for(provider))

    evidence = Evidence(tmp_path)
    planner = ModelPlanner(config(provider), evidence, transport=httpx.MockTransport(handler))
    assert (await planner.decide(context)).op == "done"
    assert planner.calls == 3 and len(delays) == 2
    assert len(set(requests)) == 1
    if status != "connect":
        assert delays == [1.5, 1.5]
    rows = [json.loads(s) for s in (evidence.directory / "events.jsonl").read_text().splitlines()]
    assert [r["event"] for r in rows] == ["model_retry", "model_retry", "model_response"]
    assert [r["llm_calls"] for r in rows] == [1, 2, 3]
    assert "PRIVATE" not in json.dumps(rows)


@pytest.mark.parametrize("advice", ["60", "tomorrow", "NaN", "Infinity", "-1"])
async def test_invalid_or_long_retry_after_stops_without_early_retry(advice, context, tmp_path):
    planner = ModelPlanner(
        config(),
        Evidence(tmp_path),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(429, headers={"Retry-After": advice})
        ),
    )
    with pytest.raises(Stop, match="model_rate_limited"):
        await planner.decide(context)
    assert planner.calls == 1


@pytest.mark.parametrize("status", [500, 503])
@pytest.mark.parametrize("retries", [0, 3])
async def test_retry_budget_is_exhausted(status, retries, context, tmp_path, monkeypatch):
    async def no_wait(_):
        pass

    monkeypatch.setattr("computer_use_replay.planner.asyncio.sleep", no_wait)
    planner = ModelPlanner(
        config(retries=retries),
        Evidence(tmp_path),
        transport=httpx.MockTransport(lambda _: httpx.Response(status)),
    )
    with pytest.raises(Stop, match="model_unavailable"):
        await planner.decide(context)
    assert planner.calls == retries + 1


async def test_backoff_and_drip_response_share_one_deadline(context, tmp_path):
    for response in [
        httpx.Response(429, headers={"Retry-After": "5"}),
        httpx.Response(500, headers={"Retry-After": "5"}),
        None,
    ]:

        async def handler(_, response=response):
            if response is not None:
                return response
            await asyncio.sleep(10)

        planner = ModelPlanner(
            config(timeout=0.02), Evidence(tmp_path), transport=httpx.MockTransport(handler)
        )
        with pytest.raises(Stop, match="model_timeout"):
            await asyncio.wait_for(planner.decide(context), 1)
        assert planner.calls == 1


async def test_cancellation_is_not_converted_to_retry_or_decision(context, tmp_path):
    entered = asyncio.Event()

    async def handler(_):
        entered.set()
        await asyncio.Event().wait()

    planner = ModelPlanner(config(), Evidence(tmp_path), transport=httpx.MockTransport(handler))
    task = asyncio.create_task(planner.decide(context))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert planner.calls == 1


async def test_oversized_response_is_rejected(context, tmp_path):
    planner = ModelPlanner(
        config(),
        Evidence(tmp_path),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=b"x" * (1024 * 1024 + 1))
        ),
    )
    with pytest.raises(Stop, match="model_response_too_large"):
        await planner.decide(context)
    assert planner.calls == 1


@pytest.mark.parametrize(
    "provider,path,value",
    [
        ("ollama", ["done"], False),
        ("ollama", ["done"], None),
        ("ollama", ["done_reason"], "length"),
        ("ollama", ["message"], None),
        ("ollama", ["message", "tool_calls"], []),
        ("ollama", ["message", "tool_calls"], [{}, {}]),
        ("ollama", ["message", "tool_calls"], None),
        ("ollama", ["prompt_eval_count"], "PRIVATE-token-count"),
        ("openai", ["status"], "incomplete"),
        ("openai", ["error"], {"message": "PRIVATE"}),
        ("openai", ["output", 1, "type"], "message"),
        ("openai", ["output", 1, "status"], "incomplete"),
        ("openai", ["output", 1, "arguments"], '{"x":1,"x":2}'),
        ("openai", ["output", 1, "arguments"], "{} trailing"),
        ("openai", ["output", 1, "arguments"], "[]"),
        ("openai", ["output"], [{"type": "reasoning"}]),
        ("openai", ["usage"], None),
        ("openai", ["usage", "input_tokens"], -1),
    ],
)
async def test_partial_refused_or_ambiguous_response_never_executes(
    provider, path, value, context, tmp_path
):
    data = response_for(provider)
    parent = data
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value
    planner = ModelPlanner(
        config(provider),
        Evidence(tmp_path),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=data)),
    )
    with pytest.raises(Stop, match="invalid_model_response"):
        await planner.decide(context)
    assert planner.calls == 1


@pytest.mark.parametrize(
    "provider,path",
    [
        ("ollama", ["message", "tool_calls"]),
        ("openai", ["output"]),
    ],
)
async def test_multiple_calls_are_rejected_even_if_each_is_valid(provider, path, context, tmp_path):
    data = response_for(provider)
    calls = data
    for key in path:
        calls = calls[key]
    calls.append(calls[-1])
    planner = ModelPlanner(
        config(provider),
        Evidence(tmp_path),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=data)),
    )
    with pytest.raises(Stop, match="invalid_model_response"):
        await planner.decide(context)
    assert planner.calls == 1


async def test_response_stream_is_closed_after_size_limit(context, tmp_path):
    class Stream(httpx.AsyncByteStream):
        closed = False
        chunks = 0

        async def __aiter__(self):
            for _ in range(20):
                self.chunks += 1
                yield b"x" * (256 * 1024)

        async def aclose(self):
            self.closed = True

    stream = Stream()
    planner = ModelPlanner(
        config(),
        Evidence(tmp_path),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)),
    )
    with pytest.raises(Stop, match="model_response_too_large"):
        await planner.decide(context)
    assert stream.closed and stream.chunks == 5


@pytest.mark.parametrize("provider", ["ollama", "openai"])
@pytest.mark.parametrize("malformed", ["json", "unoffered", "multiple"])
async def test_rejected_prediction_retries_same_tools_without_raw_feedback(
    provider, malformed, context, tmp_path
):
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            if malformed == "json":
                return httpx.Response(200, content=b"PRIVATE-REJECTED not JSON")
            raw = response_for(provider, "execute_shell", {"secret": "PRIVATE-REJECTED"})
            if malformed == "multiple":
                if provider == "ollama":
                    raw["message"]["tool_calls"] *= 2
                else:
                    raw["output"].append(raw["output"][-1])
            return httpx.Response(200, json=raw)
        assert "PRIVATE-REJECTED" not in request.content.decode()
        assert body["tools"] == bodies[0]["tools"]
        key = "messages" if provider == "ollama" else "input"
        if provider == "ollama":
            assert body[key][-1] == bodies[0][key][-1]
        else:
            assert body[key] == bodies[0][key]
        assert "No browser action was executed" in request.content.decode()
        return httpx.Response(
            200, json=response_for(provider, "click_control", {"target": "search"})
        )

    evidence = Evidence(tmp_path)
    planner = ModelPlanner(
        config(provider, decision_retries=1), evidence, transport=httpx.MockTransport(handler)
    )
    assert (await planner.decide(context)).target == "search"
    assert planner.calls == 2
    text = (evidence.directory / "events.jsonl").read_text()
    assert "PRIVATE-REJECTED" not in text
    retries = [json.loads(x) for x in text.splitlines() if json.loads(x)["event"] == "model_retry"]
    assert len(retries) == 1
    assert retries[0]["code"] == "invalid_model_response"
    assert len(retries[0]["response_sha256"]) == 64


@pytest.mark.parametrize("budget", [0, 1, 2])
async def test_rejected_predictions_exhaust_without_a_decision(budget, context, tmp_path):
    planner = ModelPlanner(
        config(decision_retries=budget),
        Evidence(tmp_path),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json=response_for("openai", "click_control", {"target": "finalize"})
            )
        ),
    )
    with pytest.raises(Stop, match="invalid_model_response"):
        await planner.decide(context)
    assert planner.calls == budget + 1


async def test_decision_retries_share_the_original_deadline(context, tmp_path):
    calls = 0

    async def handler(_):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, content=b"invalid")
        await asyncio.sleep(1)
        return httpx.Response(200)

    planner = ModelPlanner(
        config(timeout=0.03, decision_retries=2),
        Evidence(tmp_path),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(Stop, match="model_timeout"):
        await planner.decide(context)
    assert planner.calls == 2


@pytest.mark.parametrize("budget", [-1, 3, True, 1.0])
def test_decision_retry_budget_is_strictly_bounded(budget):
    with pytest.raises(ValueError, match="decision retries"):
        config(decision_retries=budget)


@pytest.mark.parametrize("provider", ["ollama", "openai"])
@pytest.mark.parametrize("operation", ["fill", "read"])
@pytest.mark.parametrize("recover", [False, True])
async def test_correlated_arguments_are_validated_before_accepting_prediction(
    provider, operation, recover, tmp_path
):
    context = {
        "inputs": {"first": {}, "second": {}},
        "outputs": {"first": {"source": "left"}, "second": {"source": "right"}},
        "catalog": {
            "left": {"operations": [operation], "risk": "reversible", "allowed_inputs": ["first"]},
            "right": {
                "operations": [operation],
                "risk": "reversible",
                "allowed_inputs": ["second"],
            },
        },
        "observation": {"controls": [{"target": k, "count": 1} for k in ["left", "right"]]},
    }
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        parameter = "first" if recover and len(requests) == 2 else "second"
        return httpx.Response(
            200,
            json=response_for(
                provider,
                operation + "_control",
                {"target": "left", "parameter" if operation == "fill" else "output": parameter},
            ),
        )

    planner = ModelPlanner(
        config(provider, decision_retries=1),
        Evidence(tmp_path),
        transport=httpx.MockTransport(handler),
    )
    if recover:
        decision = await planner.decide(context)
        assert decision.target == "left"
        assert (decision.input if operation == "fill" else decision.output) == "first"
    else:
        with pytest.raises(Stop, match="invalid_model_response"):
            await planner.decide(context)
    assert planner.calls == 2
    assert requests[0]["tools"] == requests[1]["tools"]
    # Each individual value was offered; their combination was the error.
    tool = next(
        t for t in requests[0]["tools"] if (t.get("function", t))["name"] == operation + "_control"
    )
    properties = tool.get("function", tool)["parameters"]["properties"]
    assert "left" in properties["target"]["enum"]
    assert "second" in properties["parameter" if operation == "fill" else "output"]["enum"]
