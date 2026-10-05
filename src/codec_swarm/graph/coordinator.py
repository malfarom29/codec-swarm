"""Multi-lane missions: one planning thread, then one lane thread per repo, started in dependency order.

A lane starts once every lane it depends on has reached the judge (approved, or waiting at the review
or PR gate), so a client codes against a real API branch. Lanes run side by side and pause independently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import anyio

from codec_swarm.domain import Handoff, LaneCycle, LaneOrder, Mission, lane_dependencies
from codec_swarm.graph.runner import MissionRunner, RunResult
from codec_swarm.plugins.api import EventSink

JUDGED_GATES = {"review", "pr"}
NOT_STARTED = "not_started"


@dataclass(frozen=True)
class LaneResult:
    repo: str
    status: str  # not_started | waiting | pr_ready | failed
    gate: dict[str, Any] | None = None
    pr_url: str | None = None
    error: str | None = None

    @property
    def judged(self) -> bool:
        return self.status == "pr_ready" or (self.status == "waiting" and (self.gate or {}).get("kind") in JUDGED_GATES)


@dataclass(frozen=True)
class MissionResult:
    planning: RunResult
    lanes: dict[str, LaneResult] = field(default_factory=dict)
    blocked: str | None = None  # why no lane can start, e.g. a cycle in the lane order

    @property
    def done(self) -> bool:
        return bool(self.lanes) and all(lane.status == "pr_ready" for lane in self.lanes.values())

    def waiting(self) -> list[tuple[str | None, dict[str, Any]]]:
        """Open gates as (lane, gate); lane None is the planning gate."""
        if self.planning.status == "waiting":
            return [(None, self.planning.gate or {})]
        return [(repo, lane.gate or {}) for repo, lane in self.lanes.items() if lane.status == "waiting"]


def lane_thread(ticket: str, repo: str) -> str:
    return f"{ticket}:{repo}"


def _lane_order(handoffs: list[dict[str, Any]]) -> tuple[LaneOrder, ...]:
    """The latest lane plan any planning role handed off (the Architect's)."""
    for raw in reversed(handoffs):
        order = Handoff.model_validate(raw).lane_order
        if order:
            return order
    return ()


class MissionCoordinator:
    def __init__(self, planning: MissionRunner, lanes: MissionRunner, events: EventSink) -> None:
        self._planning = planning
        self._lanes = lanes
        self._events = events

    async def start(self, mission: Mission, labels: dict[str, Any] | None = None) -> MissionResult:
        repos = mission.repos or (mission.repo,)
        plan = mission.model_copy(update={"repo": "", "repos": repos})
        result = await self._planning.start(plan, labels if labels is not None else {}, thread=mission.ticket)
        return await self._advance(plan, result)

    async def answer(self, ticket: str, lane: str | None, answer: str) -> MissionResult:
        if lane is None:
            result = await self._planning.answer(ticket, answer)
        else:
            await self._lanes.answer(lane_thread(ticket, lane), answer)
            result = await self._planning.status(ticket)
        return await self._advance(Mission.model_validate(result.mission), result)

    async def _advance(self, plan: Mission, planning: RunResult) -> MissionResult:
        if planning.status != "planned":
            return MissionResult(planning=planning, lanes={r: LaneResult(r, NOT_STARTED) for r in plan.repos})
        try:
            deps = lane_dependencies(plan.repos, _lane_order(planning.handoffs))
        except LaneCycle as cycle:
            self._events.append(plan.ticket, "mission.blocked", {"reason": f"lane order has a cycle: {cycle}"})
            return MissionResult(planning=planning, blocked=f"lane order has a cycle: {cycle}")
        lanes = {repo: await self._status(plan.ticket, repo) for repo in plan.repos}
        while True:
            ready = [r for r in plan.repos if lanes[r].status == NOT_STARTED and all(lanes[d].judged for d in deps[r])]
            if not ready:
                break
            started: dict[str, LaneResult] = {}

            async def run(repo: str) -> None:
                self._events.append(plan.ticket, "lane.started", {"lane": repo, "after": sorted(deps[repo])})
                lane = plan.model_copy(update={"repo": repo})
                try:
                    result = await self._lanes.start(lane, thread=lane_thread(plan.ticket, repo))
                    started[repo] = self._lane(repo, result)
                except Exception as error:  # one lane failing must not stop the others
                    self._events.append(plan.ticket, "lane.failed", {"lane": repo, "error": str(error)})
                    started[repo] = LaneResult(repo, "failed", error=str(error))

            async with anyio.create_task_group() as group:
                for repo in ready:
                    group.start_soon(run, repo)
            lanes.update(started)
        return MissionResult(planning=planning, lanes=lanes)

    async def _status(self, ticket: str, repo: str) -> LaneResult:
        result = await self._lanes.status(lane_thread(ticket, repo))
        return LaneResult(repo, NOT_STARTED) if result is None else self._lane(repo, result)

    @staticmethod
    def _lane(repo: str, result: RunResult) -> LaneResult:
        return LaneResult(repo, result.status, gate=result.gate, pr_url=result.pr_url)
