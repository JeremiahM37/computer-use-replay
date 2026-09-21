"""Provider wire formats and explicit configuration; no browser or execution authority."""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

# One local runtime and one cloud adapter; live evidence uses Ollama.
ENDPOINTS = {"openai": "https://api.openai.com/v1", "ollama": "http://127.0.0.1:11434"}
KEY_ENV = {"openai": "OPENAI_API_KEY"}


@dataclass(frozen=True)
class ProviderConfig:
    provider: str
    model: str
    endpoint: str
    api_key: str = field(default="", repr=False)
    timeout: float = 90
    retries: int = 2
    max_tokens: int = 2048
    decision_retries: int = 0

    def __post_init__(self):
        if self.provider not in ENDPOINTS:
            raise ValueError("unknown provider")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", self.model):
            raise ValueError("invalid model identifier")
        url = urlsplit(self.endpoint)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or url.port == 0
            or any(c.isspace() for c in self.endpoint)
            or "%" in self.endpoint
        ):
            raise ValueError("invalid model endpoint")
        if self.provider != "ollama":
            if self.endpoint != ENDPOINTS[self.provider]:
                raise ValueError("cloud credentials require the official endpoint")
            if not self.api_key:
                raise ValueError("missing provider credential")
        if self.api_key and (not self.api_key.isascii() or any(c.isspace() for c in self.api_key)):
            raise ValueError("invalid provider credential")
        if not math.isfinite(self.timeout) or not 0 < self.timeout <= 300:
            raise ValueError("timeout must be within 0 and 300 seconds")
        if type(self.retries) is not int or not 0 <= self.retries <= 3:
            raise ValueError("retries must be between 0 and 3")
        if type(self.decision_retries) is not int or not 0 <= self.decision_retries <= 2:
            raise ValueError("decision retries must be between 0 and 2")
        if type(self.max_tokens) is not int or not 128 <= self.max_tokens <= 16384:
            raise ValueError("output budget must be between 128 and 16384 tokens")

    @classmethod
    def from_env(cls, provider="ollama", model=None, endpoint=None, **kwargs):
        if provider not in ENDPOINTS:
            raise ValueError("unknown provider")
        if provider == "ollama":
            model = model or os.getenv("OLLAMA_MODEL", "qwen3.6:35b-a3b")
            endpoint = endpoint or os.getenv("OLLAMA_URL", ENDPOINTS[provider])
        if not model:
            raise ValueError("choose an explicit model with --model")
        return cls(
            provider,
            model,
            (endpoint or ENDPOINTS[provider]).rstrip("/"),
            os.getenv(KEY_ENV[provider], "") if provider in KEY_ENV else "",
            **kwargs,
        )


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("non-finite JSON number")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def encode(config, system, context, tools):
    """Return path, headers, body. API keys never appear in URLs or model messages."""
    p = config.provider
    user = json.dumps(context, allow_nan=False)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    functions = [t["function"] for t in tools]
    if p == "ollama":
        return (
            "/api/chat",
            {},
            {
                "model": config.model,
                "stream": False,
                "think": False,
                "keep_alive": "2m",
                "options": {"temperature": 0, "num_ctx": 8192, "num_predict": config.max_tokens},
                "messages": messages,
                "tools": tools,
            },
        )
    headers = {"Authorization": "Bearer " + config.api_key}
    return (
        "/responses",
        headers,
        {
            "model": config.model,
            "instructions": system,
            "input": user,
            "store": False,
            "tools": [{"type": "function", **f, "strict": True} for f in functions],
            "tool_choice": "required",
            "parallel_tool_calls": False,
            "max_output_tokens": config.max_tokens,
        },
    )


def one(items):
    if not isinstance(items, list) or len(items) != 1:
        raise ValueError("exactly one item required")
    return items[0]


def object_value(value):
    if not isinstance(value, dict):
        raise ValueError("object required")
    return value


def tokens(usage, prompt, output):
    usage = object_value(usage)
    counts = (usage.get(prompt), usage.get(output))
    for count in counts:
        if count is not None and (type(count) is not int or count < 0):
            raise ValueError("invalid token count")
    return counts


def decode(provider, data):
    """Normalize exactly one completed function call; never interpret prose as actions."""
    data = object_value(data)
    if provider == "ollama":
        if data.get("done") is not True or data.get("done_reason", "stop") != "stop":
            raise ValueError("incomplete generation")
        call = one(object_value(data["message"])["tool_calls"])
        usage = tokens(data, "prompt_eval_count", "eval_count")
    else:  # ProviderConfig already restricts callers to Ollama/OpenAI.
        if data["status"] != "completed" or data.get("error"):
            raise ValueError("incomplete generation")
        items = data["output"]
        # Reasoning is permitted, but refusals, unexpected tools, and prose are not actions.
        calls = [object_value(item) for item in items if object_value(item)["type"] != "reasoning"]
        item = one(calls)
        if item["type"] != "function_call" or item.get("status", "completed") != "completed":
            raise ValueError("function call required")
        call = {"function": {"name": item["name"], "arguments": strict_json(item["arguments"])}}
        usage = tokens(data.get("usage", {}), "input_tokens", "output_tokens")
    return call, usage
