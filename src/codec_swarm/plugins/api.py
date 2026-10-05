"""Plugin contracts. The graph depends on these Protocols only, never on a concrete plugin."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol

from pydantic import BaseModel

from codec_swarm.domain import Decision, Handoff, Mission, Pack, Verdict


class StepRequest(BaseModel, frozen=True):
    mission: Mission
    role: str
    incoming: Handoff | None = None
    model: str | None = None  # the router's pick; None keeps the role's harness default
    messages: tuple[str, ...] = ()  # what I typed to this role from the dashboard since its last step


class AgentEvent(BaseModel, frozen=True):
    """One normalized event from an agent step. A step ends with exactly one `handoff` event."""

    kind: str  # agent.message | agent.tool | cost | handoff
    role: str
    payload: dict[str, Any] = {}


class AgentBackend(Protocol):
    def run_step(self, request: StepRequest) -> AsyncIterator[AgentEvent]: ...


class Router(Protocol):
    """Both calls may be sync or async; the graph awaits whichever it gets."""

    def pick_model(self, pack: Pack, mission: Mission, role: str, incoming: Handoff | None) -> Decision: ...

    def next_role(self, pack: Pack, role: str, handoff: Handoff) -> Decision: ...


class Judge(Protocol):
    async def evaluate(self, mission: Mission, handoffs: list[Handoff]) -> Verdict: ...


class HandoffRecorder(Protocol):
    """Persists a handoff once the router has picked the next role; returns the commit sha, if any."""

    def record(self, mission: Mission, step: int, handoff: Handoff, to_role: str) -> str | None: ...


class Publisher(Protocol):
    """Pushes an approved lane and opens its PR; returns the PR URL. Must be safe to call twice."""

    def publish(self, mission: Mission, handoffs: list[Handoff], verdict: dict[str, Any] | None) -> str: ...


class ChatInbox(Protocol):
    """Messages queued for a role; taking them marks them delivered."""

    def take_pending(self, ticket: str, lane: str, role: str) -> list[Any]: ...


class EventSink(Protocol):
    """Where the engine writes its append-only event log."""

    def append(self, mission: str, kind: str, payload: dict[str, Any], role: str | None = None) -> int: ...
