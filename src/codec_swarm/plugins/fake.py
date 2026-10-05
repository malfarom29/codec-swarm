"""Scripted stand-ins for Claude Code and the judge, so graph tests spend no tokens."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from codec_swarm.domain import Handoff, Mission, Verdict
from codec_swarm.plugins.api import AgentEvent, StepRequest


class BackendCrash(RuntimeError):
    """Simulates the process dying in the middle of a step."""


@dataclass
class FakeBackend:
    send_back_once: set[str] = field(default_factory=set)  # roles whose first handoff sends the work back
    crash_on: str | None = None  # role whose step raises BackendCrash
    calls: list[str] = field(default_factory=list)
    requests: list[tuple[str, Handoff | None]] = field(default_factory=list)  # (role, incoming handoff)
    _sent_back: set[str] = field(default_factory=set)

    async def run_step(self, request: StepRequest) -> AsyncIterator[AgentEvent]:
        role = request.role
        self.calls.append(role)
        self.requests.append((role, request.incoming))
        if role == self.crash_on:
            raise BackendCrash(f"backend crashed while {role} worked on {request.mission.ticket}")
        send_back = role in self.send_back_once and role not in self._sent_back
        if send_back:
            self._sent_back.add(role)
        yield AgentEvent(kind="agent.message", role=role, payload={"text": f"{role} working on {request.mission.ticket}"})
        handoff = Handoff(from_role=role, summary=f"{role} {'sends back' if send_back else 'done'}", send_back=send_back)
        yield AgentEvent(kind="handoff", role=role, payload=handoff.model_dump())


@dataclass
class FakeJudge:
    """Returns the scripted scores in order; the last one repeats."""

    scores: list[float] = field(default_factory=lambda: [0.97])
    calls: int = 0

    async def evaluate(self, mission: Mission, handoffs: list[Handoff]) -> Verdict:
        score = self.scores[min(self.calls, len(self.scores) - 1)]
        self.calls += 1
        return Verdict(score=score, source="fake")
