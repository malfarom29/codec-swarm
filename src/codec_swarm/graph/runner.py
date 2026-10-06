"""Runs a mission graph against the SQLite checkpointer: start, answer a gate, recover after a crash."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from codec_swarm.domain import Mission, Pack
from codec_swarm.graph.build import build_graph
from codec_swarm.plugins.api import AgentBackend, ChatInbox, EventSink, HandoffRecorder, Judge, Publisher, Router


SQLITE_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class RunResult:
    status: str  # waiting | pr_ready | planned | failed (stopped mid-step)
    gate: dict[str, Any] | None = None
    trail: list[str] = field(default_factory=list)
    pr_url: str | None = None
    handoffs: list[dict[str, Any]] = field(default_factory=list)
    mission: dict[str, Any] | None = None


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
        part: str = "full",
        chat: ChatInbox | None = None,
    ):
        # Checkpoints live in their own file. An async checkpoint transaction can stay open across an await,
        # and a synchronous event-log write on the same file would then block the event loop it needs: a deadlock.
        self._db_path = Path(db_path).with_suffix(".checkpoints.db")
        self._events = events
        self._builder = build_graph(pack, backend, router, judge, events, recorder, publisher, part, chat)

    @asynccontextmanager
    async def _graph(self) -> AsyncIterator[CompiledStateGraph]:
        # Lanes run side by side and checkpoint into the same file: WAL plus a generous busy timeout.
        async with aiosqlite.connect(str(self._db_path), timeout=SQLITE_TIMEOUT_S) as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            yield self._builder.compile(checkpointer=AsyncSqliteSaver(conn))

    @staticmethod
    def _config(thread: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": thread}}

    async def start(self, mission: Mission, labels: dict[str, Any] | None = None, thread: str | None = None) -> RunResult:
        """Start a mission (or one lane of it) on its own thread; the thread defaults to the ticket."""
        thread = thread or mission.ticket
        state = {"mission": mission.model_dump(mode="json"), "reworks": 0}
        async with self._graph() as graph:
            if (await graph.aget_state(self._config(thread))).values:
                raise MissionExists(f"{thread} already has a mission")
            if labels is not None:
                self._events.append(
                    mission.ticket, "mission.started", {"repo": mission.repo, "autonomy": mission.autonomy.value, **labels}
                )
            return await self._advance(graph, thread, state)

    async def answer(self, thread: str, answer: str, note: str = "") -> RunResult:
        """Resume a gate. A note travels with a send-back as instructions for the role that picks the work up."""
        async with self._graph() as graph:
            snapshot = await graph.aget_state(self._config(thread))
            if not snapshot.interrupts:
                raise RuntimeError(f"{thread} is not waiting at a gate")
            gate = snapshot.interrupts[0].value
            ticket = snapshot.values["mission"]["ticket"]
            lane = snapshot.values["mission"]["repo"]
            self._events.append(ticket, "gate.resolved", {"kind": gate["kind"], "lane": lane, "answer": answer, **({"note": note} if note else {})})
            return await self._advance(graph, thread, Command(resume={"answer": answer, "note": note} if note else answer))

    async def recover(self, thread: str) -> RunResult:
        """Continue from the last checkpoint after the process died mid-step."""
        async with self._graph() as graph:
            snapshot = await graph.aget_state(self._config(thread))
            if snapshot.values:
                self._events.append(snapshot.values["mission"]["ticket"], "mission.recovered", {"thread": thread})
            return await self._advance(graph, thread, None)

    async def status(self, thread: str) -> RunResult | None:
        """Where a thread stands without running it; None if it never started."""
        async with self._graph() as graph:
            snapshot = await graph.aget_state(self._config(thread))
            return self._result(snapshot) if snapshot.values else None

    async def _advance(self, graph: CompiledStateGraph, thread: str, payload: Any) -> RunResult:
        config = self._config(thread)
        # "sync" durability writes each step's checkpoint before the next step starts, so a crash loses at most one step.
        await graph.ainvoke(payload, config, durability="sync")
        snapshot = await graph.aget_state(config)
        result = self._result(snapshot)
        if result.gate is not None:
            self._events.append(snapshot.values["mission"]["ticket"], "gate.opened", result.gate)
        return result

    @staticmethod
    def _result(snapshot: Any) -> RunResult:
        values = snapshot.values
        common = {"trail": list(values.get("trail", [])), "handoffs": list(values.get("handoffs", [])), "mission": values.get("mission")}
        if snapshot.interrupts:
            gate = {**snapshot.interrupts[0].value, "lane": values["mission"]["repo"]}
            return RunResult(status="waiting", gate=gate, **common)
        if snapshot.next:
            return RunResult(status="failed", **common)  # stopped mid-step: recover() continues it
        return RunResult(status=values.get("status", "unknown"), pr_url=values.get("pr_url"), **common)
