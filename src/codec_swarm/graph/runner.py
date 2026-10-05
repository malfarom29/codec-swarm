"""Runs a mission graph against the SQLite checkpointer: start, answer a gate, recover after a crash."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from codec_swarm.domain import Mission, Pack
from codec_swarm.graph.build import build_graph
from codec_swarm.plugins.api import AgentBackend, EventSink, HandoffRecorder, Judge, Publisher, Router


@dataclass(frozen=True)
class RunResult:
    status: str  # waiting | pr_ready
    gate: dict[str, Any] | None = None
    trail: list[str] = field(default_factory=list)
    pr_url: str | None = None


class MissionExists(RuntimeError):
    """The ticket already has a mission in the checkpointer; answer or recover it instead."""


class MissionRunner:
    """One graph definition; every call opens the checkpointer, so a new runner can pick up any mission."""

    def __init__(
        self,
        db_path: Path,
        pack: Pack,
        backend: AgentBackend,
        router: Router,
        judge: Judge,
        events: EventSink,
        recorder: HandoffRecorder | None = None,
        publisher: Publisher | None = None,
    ):
        self._db_path = db_path
        self._events = events
        self._builder = build_graph(pack, backend, router, judge, events, recorder, publisher)

    @asynccontextmanager
    async def _graph(self) -> AsyncIterator[CompiledStateGraph]:
        async with AsyncSqliteSaver.from_conn_string(str(self._db_path)) as saver:
            yield self._builder.compile(checkpointer=saver)

    @staticmethod
    def _config(ticket: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": ticket}}

    async def start(self, mission: Mission, labels: dict[str, Any] | None = None) -> RunResult:
        state = {"mission": mission.model_dump(mode="json"), "reworks": 0}
        async with self._graph() as graph:
            if (await graph.aget_state(self._config(mission.ticket))).values:
                raise MissionExists(f"{mission.ticket} already has a mission")
            self._events.append(
                mission.ticket, "mission.started", {"repo": mission.repo, "autonomy": mission.autonomy.value, **(labels or {})}
            )
            return await self._advance(graph, mission.ticket, state)

    async def answer(self, ticket: str, answer: str) -> RunResult:
        async with self._graph() as graph:
            snapshot = await graph.aget_state(self._config(ticket))
            if not snapshot.interrupts:
                raise RuntimeError(f"{ticket} is not waiting at a gate")
            gate = snapshot.interrupts[0].value
            self._events.append(ticket, "gate.resolved", {"kind": gate["kind"], "answer": answer})
            return await self._advance(graph, ticket, Command(resume=answer))

    async def recover(self, ticket: str) -> RunResult:
        """Continue from the last checkpoint after the process died mid-step."""
        self._events.append(ticket, "mission.recovered", {})
        async with self._graph() as graph:
            return await self._advance(graph, ticket, None)

    async def _advance(self, graph: CompiledStateGraph, ticket: str, payload: Any) -> RunResult:
        config = self._config(ticket)
        # "sync" durability writes each step's checkpoint before the next step starts, so a crash loses at most one step.
        await graph.ainvoke(payload, config, durability="sync")
        snapshot = await graph.aget_state(config)
        trail = list(snapshot.values.get("trail", []))
        if snapshot.interrupts:
            gate = snapshot.interrupts[0].value
            self._events.append(ticket, "gate.opened", gate)
            return RunResult(status="waiting", gate=gate, trail=trail)
        return RunResult(status=snapshot.values.get("status", "unknown"), trail=trail, pr_url=snapshot.values.get("pr_url"))
