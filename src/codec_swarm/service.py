"""Mission service: builds everything a mission needs and drives it. Shared by the CLI and the web UI.

A mission's request is stored in its `mission.started` event, so any process (a restarted server,
`codec-swarm mission --recover`) can rebuild the runtime and keep answering its gates.
"""

from __future__ import annotations

import os
import shlex
from datetime import UTC, datetime
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
from codec_swarm.plugins.jira import EXAMPLE_TICKETS, JiraClient, Ticket
from codec_swarm.plugins.registry import build_plugins, jev_available
from codec_swarm.store import EventLog, GateCache, SessionStore
from codec_swarm.store.chat import ChatStore
from codec_swarm.store.settings import JiraSettings, Settings
from codec_swarm.workspace import Workspace, WorkspaceRecorder
from codec_swarm.workspace.github import GitHubPublisher
from codec_swarm.workspace.lanes import DEFAULT_ROOT
from codec_swarm.workspace.repos import RepoCatalog
from codec_swarm.workspace.repos import repo_name as repo_name
from codec_swarm.workspace.secrets import load_secrets, remove_secret, save_secret

JIRA_TOKEN = "JIRA_API_TOKEN"


class MissionRequest(BaseModel, frozen=True):
    ticket: str
    title: str
    repo_urls: tuple[str, ...]
    description: str = ""
    autonomy: Autonomy = Autonomy.GATED
    pack: str = "auto"  # auto (rule, or Jev when on) | solo | codec-standard | a pack path
    model: str | None = None  # a local override for every role
    no_jev: bool = False


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
        self.settings = Settings(self.db)
        self.chat = ChatStore(self.db)
        self.jira_transport: Any = None  # tests swap in an httpx.MockTransport
        load_secrets(self.root)
        self._workspace = Workspace(self.root)
        self.repos = RepoCatalog(self.root, self._workspace, load_pack("codec-standard"))
        self._runtimes: dict[str, MissionRuntime] = {}

    async def build(self, request: MissionRequest, pack_name: str | None = None) -> MissionRuntime:
        standard = load_pack("codec-standard")
        repos = [(url, repo_name(url)) for url in request.repo_urls]
        configs = {name: load_repo_config(self._workspace.clone(url, name), standard, local_dir=self.repos.local_dir) for url, name in repos}
        primary = configs[repos[0][1]]
        names = tuple(name for _, name in repos)
        mission = Mission(
            ticket=request.ticket, repo="", repos=names, title=request.title, description=request.description,
            autonomy=request.autonomy, bands=primary.judge,
        )
        jev = JevClient()
        orchestration = self.settings.orchestration()
        use_jev = not request.no_jev and orchestration.jev_enabled and jev_available()
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

        band = {"gate_threshold": orchestration.gate_threshold, "gate_margin": orchestration.gate_margin}
        plugins = build_plugins(pack, primary, lane_for, self._cache, use_jev=use_jev, ask=ask, **band)
        gate = PerRepoGate({n: build_plugins(pack, configs[n], lane_for, self._cache, use_jev, ask, **band).gate for n in names}, plugins.gate)

        def overrides_for(role: str) -> LocalOverrides:
            return self.local_overrides(pack.pack.name, role, request.model)

        def session_for(m: Mission, role: str):
            if m.repo:  # a lane role works in its repo's worktree
                return resolve_session(pack, configs[m.repo], role, lanes[(m.ticket, m.repo)].path, m, overrides=overrides_for(role))
            return resolve_session(pack, primary, role, self._workspace.mission_dir(m.ticket), m, overrides=overrides_for(role))

        backend = ClaudeCodeBackend(session_for, gate, self._sessions)
        recorder, publisher = WorkspaceRecorder(self._workspace, lanes), GitHubPublisher(lanes)

        def runner(part: str) -> MissionRunner:
            return MissionRunner(self.db, pack.pack, backend, plugins.router, plugins.judge, self.events, recorder=recorder, publisher=publisher, part=part, chat=self.chat)

        coordinator = MissionCoordinator(runner("planning"), runner("lane"), self.events, pusher=publisher)
        runtime = MissionRuntime(request, mission, pack_name, plugins.jev, coordinator, jev)
        self._runtimes[request.ticket] = runtime
        return runtime

    def local_overrides(self, pack: str, role: str, forced_model: str | None = None) -> LocalOverrides:
        """The Harness page's tweaks for one role, plus a model forced for the whole mission."""
        local = self.settings.role_override(pack, role)
        return LocalOverrides(model=forced_model, default_model=local.model, extra_mcp=tuple(local.extra_mcp), extra_skills=tuple(local.extra_skills))

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

    # --- talking to agents ----------------------------------------------------

    def send_message(self, ticket: str, lane: str, role: str, text: str) -> None:
        """Queue a message for a role; it joins that role's prompt when its next step starts."""
        self.chat.send(ticket, lane, role, text)
        self.events.append(ticket, "chat.message", {"lane": lane, "text": text}, role=role)

    def attach_command(self, ticket: str, lane: str, role: str) -> str | None:
        """The shell command that opens this role's Claude Code session where it worked, or None before it ran."""
        session = self._sessions.get(ticket, lane, role)
        if session is None:
            return None
        cwd = self._workspace.worktrees / ticket / lane if lane else self._workspace.mission_dir(ticket)
        return f"cd {shlex.quote(str(cwd))} && claude --resume {shlex.quote(session)}"

    # --- Jira intake ------------------------------------------------------------

    def jira_connected(self) -> bool:
        jira = self.settings.jira()
        return bool(jira.site and jira.email and os.environ.get(JIRA_TOKEN))

    def _jira(self, jira: JiraSettings, token: str | None = None) -> JiraClient:
        return JiraClient(jira.site, jira.email, token or os.environ.get(JIRA_TOKEN, ""), transport=self.jira_transport)

    def connect_jira(self, site: str, email: str, token: str, jql: str | None = None) -> str:
        """Check the credentials, then keep the token in the root's .env and the rest in settings. Returns who connected."""
        site = site.strip().rstrip("/")
        if not site.startswith("https://"):
            site = f"https://{site.removeprefix('http://')}"
        jira = JiraSettings(site=site, email=email.strip(), jql=(jql or "").strip() or JiraSettings().jql)
        who = self._jira(jira, token.strip()).myself()  # raises JiraError before anything is saved
        save_secret(self.root, JIRA_TOKEN, token.strip())
        self.settings.set_jira(jira)
        self.sync_jira()
        return who

    def disconnect_jira(self) -> None:
        remove_secret(self.root, JIRA_TOKEN)
        self.settings.put("jira.tickets", [])

    def set_jql(self, jql: str) -> None:
        self.settings.set_jira(self.settings.jira().model_copy(update={"jql": jql.strip() or JiraSettings().jql}))

    def sync_jira(self) -> list[Ticket]:
        tickets = self._jira(self.settings.jira()).search(self.settings.jira().jql)
        self.settings.put("jira.tickets", [t.model_dump() for t in tickets])
        self.settings.put("jira.synced_at", utc_now())
        return tickets

    def intake(self) -> list[Ticket]:
        """Tickets I could start: synced from Jira once connected, examples until then; never ones already started."""
        started = {e.mission for e in self.events.list() if e.kind == "mission.started"}
        tickets = [Ticket.model_validate(t) for t in self.settings.get("jira.tickets", [])] if self.jira_connected() else EXAMPLE_TICKETS
        return [t for t in tickets if t.key not in started]

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


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def summarize(result: MissionResult) -> dict[str, Any]:
    return {
        "planning": result.planning.status,
        "blocked": result.blocked,
        "lanes": {repo: {"status": lane.status, "gate": (lane.gate or {}).get("kind"), "pr": lane.pr_url} for repo, lane in result.lanes.items()},
    }
