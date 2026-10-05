"""Mission service: builds everything a mission needs and drives it. Shared by the CLI and the web UI.

A mission's request is stored in its `mission.started` event, so any process (a restarted server,
`codec-swarm mission --recover`) can rebuild the runtime and keep answering its gates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
from pydantic import BaseModel

from codec_swarm.domain import Autonomy, Mission, choose_pack_rule
from codec_swarm.graph import MissionRunner
from codec_swarm.graph.coordinator import MissionCoordinator, MissionResult
from codec_swarm.harness import LocalOverrides, load_pack, load_repo_config, resolve_session
from codec_swarm.plugins.claude_code import ClaudeCodeBackend
from codec_swarm.plugins.gate import PerRepoGate
from codec_swarm.plugins.jev import JevClient, LaneUnderJudgement
from codec_swarm.plugins.jev.packs import choose_pack_jev
from codec_swarm.plugins.registry import build_plugins, jev_available
from codec_swarm.store import EventLog, GateCache, SessionStore
from codec_swarm.workspace import Workspace, WorkspaceRecorder
from codec_swarm.workspace.github import GitHubPublisher
from codec_swarm.workspace.lanes import DEFAULT_ROOT


class MissionRequest(BaseModel, frozen=True):
    ticket: str
    title: str
    repo_urls: tuple[str, ...]
    description: str = ""
    autonomy: Autonomy = Autonomy.GATED
    pack: str = "auto"  # auto (rule, or Jev when on) | solo | codec-standard | a pack path
    model: str | None = None  # a local override for every role
    no_jev: bool = False


def repo_name(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")


@dataclass
class MissionRuntime:
    request: MissionRequest
    mission: Mission
    pack_name: str
    jev_on: bool
    coordinator: MissionCoordinator
    jev: JevClient
    lock: anyio.Lock = field(default_factory=anyio.Lock)  # one operation at a time per mission


class MissionService:
    def __init__(self, root: Path = DEFAULT_ROOT) -> None:
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "swarm.db"
        self.events = EventLog(self.db)
        self._sessions = SessionStore(self.db)
        self._cache = GateCache(self.db)
        self._workspace = Workspace(self.root)
        self._runtimes: dict[str, MissionRuntime] = {}

    async def build(self, request: MissionRequest, pack_name: str | None = None) -> MissionRuntime:
        standard = load_pack("codec-standard")
        repos = [(url, repo_name(url)) for url in request.repo_urls]
        configs = {name: load_repo_config(self._workspace.clone(url, name), standard) for url, name in repos}
        primary = configs[repos[0][1]]
        names = tuple(name for _, name in repos)
        mission = Mission(
            ticket=request.ticket, repo="", repos=names, title=request.title, description=request.description,
            autonomy=request.autonomy, bands=primary.judge,
        )
        jev = JevClient()
        use_jev = not request.no_jev and jev_available()
        if pack_name is None:
            pack_name = request.pack
            if pack_name == "auto":
                sensitive = any(c.sensitive for c in configs.values())
                if use_jev:
                    choice = await choose_pack_jev(jev, mission, len(names), sensitive)
                else:
                    choice = choose_pack_rule(bool(mission.description.strip()), len(names), sensitive)
                pack_name = choice.next
        pack = load_pack(pack_name)
        lanes = {
            (request.ticket, name): self._workspace.prepare_lane(url, name, request.ticket, request.title, configs[name].branch_flow)
            for url, name in repos
        }
        judged = {
            name: LaneUnderJudgement(worktree=lanes[(request.ticket, name)].path, base=lanes[(request.ticket, name)].base, checks=configs[name].checks)
            for name in names
        }
        ask = jev if use_jev else None

        def lane_for(m: Mission) -> LaneUnderJudgement:
            return judged[m.repo]

        plugins = build_plugins(pack, primary, lane_for, self._cache, use_jev=use_jev, ask=ask)
        gate = PerRepoGate({n: build_plugins(pack, configs[n], lane_for, self._cache, use_jev, ask).gate for n in names}, plugins.gate)
        overrides = LocalOverrides(model=request.model)

        def session_for(m: Mission, role: str):
            if m.repo:  # a lane role works in its repo's worktree
                return resolve_session(pack, configs[m.repo], role, lanes[(m.ticket, m.repo)].path, m, overrides=overrides)
            return resolve_session(pack, primary, role, self._workspace.mission_dir(m.ticket), m, overrides=overrides)

        backend = ClaudeCodeBackend(session_for, gate, self._sessions)
        recorder, publisher = WorkspaceRecorder(self._workspace, lanes), GitHubPublisher(lanes)

        def runner(part: str) -> MissionRunner:
            return MissionRunner(self.db, pack.pack, backend, plugins.router, plugins.judge, self.events, recorder=recorder, publisher=publisher, part=part)

        coordinator = MissionCoordinator(runner("planning"), runner("lane"), self.events, pusher=publisher)
        runtime = MissionRuntime(request, mission, pack_name, plugins.jev, coordinator, jev)
        self._runtimes[request.ticket] = runtime
        return runtime

    def stored_request(self, ticket: str) -> tuple[MissionRequest, str] | None:
        """The request and pack a mission was started with, from its mission.started event."""
        for e in self.events.list(ticket):
            if e.kind == "mission.started" and "request" in e.payload:
                return MissionRequest.model_validate(e.payload["request"]), e.payload["pack"]
        return None

    async def runtime(self, ticket: str) -> MissionRuntime:
        if ticket in self._runtimes:
            return self._runtimes[ticket]
        stored = self.stored_request(ticket)
        if stored is None:
            raise KeyError(f"{ticket} has no recorded mission")
        return await self.build(*stored)

    async def start(self, request: MissionRequest) -> MissionResult:
        runtime = self._runtimes.get(request.ticket) or await self.build(request)
        labels = {"pack": runtime.pack_name, "jev": runtime.jev_on, "repos": list(runtime.mission.repos), "request": request.model_dump(mode="json")}
        async with runtime.lock:
            try:
                return await runtime.coordinator.start(runtime.mission, labels)
            finally:
                self._log_jev(runtime)

    async def answer(self, ticket: str, lane: str | None, answer: str) -> MissionResult:
        runtime = await self.runtime(ticket)
        async with runtime.lock:
            try:
                return await runtime.coordinator.answer(ticket, lane, answer)
            finally:
                self._log_jev(runtime)

    async def recover(self, ticket: str) -> MissionResult:
        runtime = await self.runtime(ticket)
        async with runtime.lock:
            try:
                return await runtime.coordinator.recover(ticket)
            finally:
                self._log_jev(runtime)

    def _log_jev(self, runtime: MissionRuntime) -> None:
        """Record Jev usage since the last record; the metrics add these up."""
        calls = runtime.jev.calls
        if calls:
            tokens = sum((c.get("input_tokens") or 0) + (c.get("output_tokens") or 0) for c in calls)
            self.events.append(runtime.request.ticket, "jev.usage", {"calls": len(calls), "tokens": tokens})
            runtime.jev.calls = []

    async def aclose(self) -> None:
        for runtime in self._runtimes.values():
            await runtime.jev.aclose()


def summarize(result: MissionResult) -> dict[str, Any]:
    return {
        "planning": result.planning.status,
        "blocked": result.blocked,
        "lanes": {repo: {"status": lane.status, "gate": (lane.gate or {}).get("kind"), "pr": lane.pr_url} for repo, lane in result.lanes.items()},
    }
