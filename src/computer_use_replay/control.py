"""One in-process owner per live session. Resume never means 'assume it worked'."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from uuid import uuid4

from computer_use_replay.evidence import Event, Evidence
from computer_use_replay.policy import Stop


class Ownership:
    def __init__(self, evidence: Evidence):
        self.lock = asyncio.Lock()
        self.owner = "automation"
        self.session_id = uuid4().hex
        self.intervention_id = None
        self.lease = None
        self.evidence = evidence
        self.capability = None
        self.reason = None
        self.step = None
        self.manual_targets = set()
        self.unmapped_manual_actions = 0

    def require_automation(self):
        if self.owner != "automation":
            raise Stop("ownership_conflict", "automation owns session", self.owner)

    async def pause(self, code, step):
        async with self.lock:
            self.require_automation()
            self.owner = "paused"
            self.reason, self.step = code, step
            self.intervention_id = uuid4().hex
            self.evidence.emit(
                Event(
                    event="intervention_requested",
                    capability=self.capability,
                    evidence=self.evidence.last_snapshot,
                    code=code,
                    step=step,
                    owner="paused",
                    intervention_id=self.intervention_id,
                    session_id=self.session_id,
                )
            )
            return self.intervention_id

    async def claim(self, intervention_id):
        async with self.lock:
            if self.owner != "paused" or intervention_id != self.intervention_id:
                raise Stop("stale_intervention")
            self.owner = "human"
            self.lease = uuid4().hex
            self.evidence.emit(
                Event(
                    event="control_claimed",
                    owner="human",
                    intervention_id=self.intervention_id,
                    session_id=self.session_id,
                )
            )
            return self.lease

    async def resume(self, lease, validate: Callable[[], Awaitable[bool]]):
        async with self.lock:
            if self.owner != "human" or not lease or lease != self.lease:
                raise Stop("stale_lease")
            if not await validate():
                raise Stop("resume_condition_unmet", "blocking state resolved", "still blocked")
            self.owner = "automation"
            self.lease = None
            self.evidence.emit(
                Event(
                    event="control_resumed",
                    owner="automation",
                    intervention_id=self.intervention_id,
                    session_id=self.session_id,
                )
            )

    async def cancel(self):
        async with self.lock:
            self.owner = "closed"
            self.lease = None
            self.evidence.emit(
                Event(event="control_cancelled", owner="closed", session_id=self.session_id)
            )


Operator = Callable[[Ownership, str, Callable[[], Awaitable[bool]]], Awaitable[None]]


class Handoff:
    def __init__(self, ownership: Ownership, operator: Operator | None = None, timeout=120.0):
        self.ownership, self.operator, self.timeout = ownership, operator, timeout

    async def intervene(self, code, step, validate):
        request = await self.ownership.pause(code, step)
        if self.operator is None:
            raise Stop(
                "operator_unavailable", "operator attached to live session", "request emitted"
            )
        try:
            async with asyncio.timeout(self.timeout):
                await self.operator(self.ownership, request, validate)
            # An operator MUST explicitly claim and request resume; callback return isn't consent.
            if self.ownership.owner != "automation" or not await validate():
                raise Stop("resume_condition_unmet")
        except BaseException:
            await self.ownership.cancel()
            raise
