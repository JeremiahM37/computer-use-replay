"""Persistence is an allowlisted projection, not regex cleaning of raw page/model data."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_serializer

from computer_use_replay.contracts import Name, Strict, Target


class Node(Strict):
    target: Name
    count: int
    ready: bool = True
    locator: Target | None = None
    box: dict[str, float] | None = None
    # Names come from the reviewed binding. No DOM text, input values, URLs or attributes.
    data: Literal["omitted"] = "omitted"


class LiveCandidate(Strict):
    """Bounded, sanitized projection of one currently visible live control."""

    candidate_id: Name
    scope: Name
    kind: Literal["button", "link", "field"]
    role: str
    label: str | None = None
    frame: tuple[str, ...] = ()
    ready: bool = True
    # Runtime-only target is deliberately excluded from serialization/model view.
    locator: Target | None = None

    @model_serializer(mode="wrap")
    def serialized(self, handler):
        data = handler(self)
        data.pop("locator", None)
        return data


class Snapshot(Strict):
    controls: tuple[Node, ...]
    states: tuple[Name, ...]
    unknown_dialogs: int = 0
    live_candidates: tuple[LiveCandidate, ...] = ()

    @model_serializer(mode="wrap")
    def serialized(self, handler):
        data = handler(self)
        if not self.live_candidates:
            data.pop("live_candidates", None)
        return data


class Event(Strict):
    event: Literal[
        "started",
        "observation",
        "decision",
        "action_started",
        "action_completed",
        "recovery",
        "intervention_requested",
        "control_claimed",
        "control_resumed",
        "control_cancelled",
        "operator_action",
        "success",
        "business_outcome",
        "failure",
        "model_response",
        "model_retry",
        "artifact_saved",
        "completion_verified",
        "checkpoint_observed",
        "presentation_drift",
        "alternate_resolved",
        "fallback_resolved",
        "contract_proposed",
        "contract_accepted",
    ]
    capability: Name | None = None
    evidence: str | None = None
    step: int | None = None
    target: Name | None = None
    parameter: Name | None = None
    op: Literal["click", "fill", "read", "done", "stop"] | None = None
    code: Name | None = None
    owner: Literal["automation", "paused", "human", "closed"] | None = None
    intervention_id: str | None = None
    session_id: str | None = None
    provider: Literal["openai", "ollama"] | None = None
    model: str | None = None
    llm_calls: int | None = None
    elapsed_ms: int | None = None
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    response_sha256: str | None = None
    artifact_sha256: str | None = None
    action_kind: Literal["click", "input", "submit", "navigation"] | None = None
    sequence: int = Field(default=0, ge=0)
    # presentation_drift only: reviewed target keys whose recorded discovery-time
    # locator hint no longer matches what the current presentation resolves.
    targets: tuple[Name, ...] | None = None
    count: int | None = Field(default=None, ge=0)
    # alternate_resolved only: which reviewed rung resolved `target` (1 = first
    # alternate, 2 = second, ...). The primary rung (0) is never logged.
    rank: int | None = Field(default=None, ge=1)
    # fallback_resolved only: which verified-ladder rung rescued `target` for
    # this step's own acting-time resolution. Never the matched text itself.
    # "normalized" is the only rung that ever acts -- see fallback.py.
    rung: Literal["normalized"] | None = None


def atomic_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class Evidence:
    def __init__(self, root: Path, run_id: str | None = None):
        self.run_id = run_id or uuid4().hex
        self.directory = root / self.run_id
        self.directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.sequence = 0
        self.last_snapshot = None

    def emit(self, event: Event):
        self.sequence += 1
        row = event.model_dump(exclude_none=True)
        row.update(sequence=self.sequence, time=datetime.now(UTC).isoformat())
        fd = os.open(self.directory / "events.jsonl", os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(row) + "\n")

    def snapshot(self, snapshot: Snapshot) -> str:
        name = f"state-{self.sequence:04d}.json"
        atomic_json(self.directory / name, snapshot.model_dump(mode="json"))
        self.last_snapshot = name
        return name

    def result(self, result):
        row = result.model_dump(mode="json")
        if "outputs" in row:
            row["outputs"] = {k: "<withheld>" for k in row["outputs"]}
        atomic_json(self.directory / "result.json", row)
