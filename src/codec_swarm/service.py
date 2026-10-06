"""Mission service: builds everything a mission needs and drives it. Shared by the CLI and the web UI.

A mission's request is stored in its `mission.started` event, so any process (a restarted server,
`codec-swarm mission --recover`) can rebuild the runtime and keep answering its gates.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from datetime import UTC, datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
from pydantic import BaseModel

from codec_swarm.domain import Autonomy, Mission, choose_pack_rule
from codec_swarm.graph import APPROVE, SEND_BACK, MissionRunner
from codec_swarm.graph.coordinator import MissionCoordinator, MissionResult, lane_thread
from codec_swarm.graph.runner import forget_threads
from codec_swarm.harness import BranchFlow, LocalOverrides, load_pack, load_repo_config, resolve_session
from codec_swarm.harness.config import CommitRules
from codec_swarm.plugins.claude_code import ClaudeCodeBackend
from codec_swarm.plugins.checks import run_check
from codec_swarm.plugins.gate import PerRepoGate
from codec_swarm.plugins.jev import JevClient, LaneUnderJudgement
from codec_swarm.plugins.jev.packs import choose_pack_jev
from codec_swarm.plugins.jira import EXAMPLE_TICKETS, JiraClient, Ticket
from codec_swarm.plugins.registry import build_plugins, jev_available
from codec_swarm.store import EventLog, GateCache, SessionStore
from codec_swarm.store.chat import ChatStore
from codec_swarm.store.views import mission_view
from codec_swarm.store.settings import JiraSettings, Settings
from codec_swarm.workspace import Workspace, WorkspaceRecorder
from codec_swarm.workspace.github import GitHubPublisher, LocalPR, merge_for_resolution, squash, squash_message, update_from_base
from codec_swarm.workspace.envs import DEFAULT_FILE as DEFAULT_ENV_FILE
from codec_swarm.workspace.envs import FILE_NAME as ENV_FILE_NAME
from codec_swarm.workspace.envs import RepoEnvs, write_env_file
from codec_swarm.workspace.lanes import DEFAULT_ROOT, Lane
from codec_swarm.workspace.repos import RepoCatalog
from codec_swarm.workspace.repos import repo_name as repo_name
from codec_swarm.workspace.secrets import load_secrets, remove_secret, save_secret

JIRA_TOKEN = "JIRA_API_TOKEN"


class MissionBusy(RuntimeError):
    """The mission is in the middle of a step; the action must wait for a gate."""


class MissionRequest(BaseModel, frozen=True):
    ticket: str
    title: str
    repo_urls: tuple[str, ...]
    description: str = ""
    autonomy: Autonomy = Autonomy.GATED
    pack: str = "auto"  # auto (rule, or Jev when on) | solo | codec-standard | a pack path
    model: str | None = None  # a local override for every role
    no_jev: bool = False
    bases: dict[str, str] = {}  # repo name -> branch the lane starts from, replacing its branch_flow.base
    env_overrides: dict[str, list[str]] = {}  # repo name -> variables overridden for this mission (names only; values in env/)


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
        self.envs = RepoEnvs(self.root)
        self.jira_transport: Any = None  # tests swap in an httpx.MockTransport
        load_secrets(self.root)
        self._workspace = Workspace(self.root)
        self.repos = RepoCatalog(self.root, self._workspace, load_pack("codec-standard"))
        self._runtimes: dict[str, MissionRuntime] = {}

    async def build(self, request: MissionRequest, pack_name: str | None = None) -> MissionRuntime:
        standard = load_pack("codec-standard")
        repos = [(url, repo_name(url)) for url in request.repo_urls]
        def clone_all() -> dict[str, Any]:  # git clone/fetch: off the event loop, so the dashboard stays responsive
            return {name: load_repo_config(self._workspace.clone(url, name), standard, local_dir=self.repos.local_dir) for url, name in repos}

        configs = await anyio.to_thread.run_sync(clone_all)
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
        flows = {name: self._flow(name, configs[name].branch_flow, request.bases.get(name)) for _, name in repos}

        def prepare_all() -> dict[tuple[str, str], Any]:
            return {
                (request.ticket, name): self._workspace.prepare_lane(url, name, request.ticket, request.title, flows[name])
                for url, name in repos
            }

        lanes = await anyio.to_thread.run_sync(prepare_all)
        lane_env = {name: self.envs.for_lane(request.ticket, name) for name in names}

        def write_env_files() -> dict[str, str | None]:
            return {name: write_env_file(lanes[(request.ticket, name)].path, self.env_file(name), lane_env[name]) for name in names}

        for name, note in (await anyio.to_thread.run_sync(write_env_files)).items():
            if note and not any(e.kind == "env.note" and e.payload.get("lane") == name for e in self.events.list(request.ticket)):
                self.events.append(request.ticket, "env.note", {"lane": name, "note": note})
        judged = {
            name: LaneUnderJudgement(
                worktree=lanes[(request.ticket, name)].path, base=lanes[(request.ticket, name)].base, checks=configs[name].checks, env=lane_env[name],
            )
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
            if m.repo:  # a lane role works in its repo's worktree, with its repo's environment
                spec = resolve_session(pack, configs[m.repo], role, lanes[(m.ticket, m.repo)].path, m, overrides=overrides_for(role))
                return spec.model_copy(update={"env": lane_env[m.repo], "env_file": self.env_file(m.repo)})
            return resolve_session(pack, primary, role, self._workspace.mission_dir(m.ticket), m, overrides=overrides_for(role))

        backend = ClaudeCodeBackend(session_for, gate, self._sessions)
        recorder, publisher = WorkspaceRecorder(self._workspace, lanes), GitHubPublisher(lanes, record_dir=self._workspace.record_dir, commit_rules={n: configs[n].commit for n in names})

        def runner(part: str) -> MissionRunner:
            return MissionRunner(self.db, pack.pack, backend, plugins.router, plugins.judge, self.events, recorder=recorder, publisher=publisher, part=part, chat=self.chat)

        coordinator = MissionCoordinator(runner("planning"), runner("lane"), self.events, pusher=publisher)
        runtime = MissionRuntime(request, mission, pack_name, plugins.jev, coordinator, jev)
        self._runtimes[request.ticket] = runtime
        return runtime

    def env_file(self, repo: str) -> str:
        """The file a lane's environment is written to: .env unless I set another for this repo."""
        return self.settings.get(f"env.{repo}.file") or DEFAULT_ENV_FILE

    def set_env_file(self, repo: str, name: str) -> None:
        name = name.strip() or DEFAULT_ENV_FILE
        if not ENV_FILE_NAME.fullmatch(name) or name in (".git", ".swarm"):
            raise ValueError(f"{name!r} is not a file name codec-swarm can write")
        self.settings.put(f"env.{repo}.file", name)

    def _flow(self, name: str, flow: BranchFlow, base: str | None) -> BranchFlow:
        """The repo's branch flow, starting from the base I picked for this mission if I picked one."""
        if not base or base == flow.base:
            return flow
        branches = self.repos.branches(name)
        if base not in branches:
            raise ValueError(f"{name} has no branch {base!r} on origin")
        return flow.model_copy(update={"base": base})

    def local_overrides(self, pack: str, role: str, forced_model: str | None = None) -> LocalOverrides:
        """The Harness page's tweaks for one role, plus a model forced for the whole mission."""
        local = self.settings.role_override(pack, role)
        return LocalOverrides(model=forced_model, default_model=local.model, extra_mcp=tuple(local.extra_mcp), extra_skills=tuple(local.extra_skills))

    def stored_request(self, ticket: str) -> tuple[MissionRequest, str] | None:
        """The request and pack a mission was last started with, from its latest mission.started event."""
        for e in reversed(self.events.list(ticket)):
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

    async def answer(self, ticket: str, lane: str | None, answer: str, note: str = "") -> MissionResult:
        runtime = await self.runtime(ticket)
        async with runtime.lock:
            try:
                return await runtime.coordinator.answer(ticket, lane, answer, note)
            finally:
                self._log_jev(runtime)

    def is_busy(self, ticket: str) -> bool:
        """A step, gate answer or recovery is running for this mission right now."""
        runtime = self._runtimes.get(ticket)
        return bool(runtime and runtime.lock.locked())

    async def restart(self, ticket: str, request: MissionRequest | None = None) -> MissionResult:
        """Throw away the mission's worktrees, branches, checkpoints and sessions, then run it again from the start."""
        if self.is_busy(ticket):
            raise MissionBusy(f"{ticket} is working right now; restart it once it waits at a gate or stops")
        stored = self.stored_request(ticket)
        if request is None:
            if stored is None:
                raise KeyError(f"{ticket} has no recorded mission")
            request = stored[0]
        repos = {repo_name(u) for u in request.repo_urls} | ({repo_name(u) for u in stored[0].repo_urls} if stored else set())
        self._runtimes.pop(ticket, None)
        await forget_threads(self.db, [ticket, *(lane_thread(ticket, r) for r in sorted(repos))])
        removed = await anyio.to_thread.run_sync(self._workspace.discard, ticket)
        self._sessions.forget(ticket)
        self.chat.forget_pending(ticket)
        self.events.append(ticket, "mission.restarted", {"removed": removed})
        return await self.start(request)

    async def restart_lane(self, ticket: str, repo: str, note: str = "") -> MissionResult:
        """Run one lane again from its first role, on a fresh worktree; the approved spec and the other lanes stay.
        A note reaches that first role with its step's prompt."""
        if self.is_busy(ticket):
            raise MissionBusy(f"{ticket} is working right now; restart the lane once it waits at a gate or stops")
        stored = self.stored_request(ticket)
        if stored is None or repo not in {repo_name(u) for u in stored[0].repo_urls}:
            raise KeyError(f"{ticket} has no lane for {repo}")
        self._runtimes.pop(ticket, None)  # rebuilt below, preparing a fresh worktree for this lane
        await forget_threads(self.db, [lane_thread(ticket, repo)])
        branch = await anyio.to_thread.run_sync(self._workspace.discard_lane, ticket, repo)
        self._sessions.forget(ticket, repo)
        self.events.append(ticket, "lane.restarted", {"lane": repo, "branch": branch, **({"note": note} if note.strip() else {})})
        runtime = await self.runtime(ticket)
        if note.strip():
            self.chat.send(ticket, repo, load_pack(runtime.pack_name).pack.lane_roles[0], note.strip())
        return await self.recover(ticket)

    async def update_from_base(self, ticket: str, repo: str, resolve_with_coder: bool = False) -> str:
        """Rebase a lane on its newest base. Clean: refresh its local PR and rerun the checks. Conflict: back out,
        or, at an open PR or review gate, merge with the markers left in and send the lane back to the coder."""
        if self.is_busy(ticket):
            raise MissionBusy(f"{ticket} is working right now; update it once it waits at a gate")
        lane, pr = self._lane(ticket, repo)
        result = await anyio.to_thread.run_sync(update_from_base, lane)
        if result.up_to_date:
            if pr is not None and pr.sha is None and self.lane_state(pr):  # heal an interrupted squash
                loaded = self.prs.load(ticket, repo)
                body = loaded[1] if loaded else ""
                changed = body.split("## What changed", 1)[-1].split("\n## ", 1)[0].split("<details>", 1)[0].strip() if "## What changed" in body else ""
                messages = [e.payload.get("commit_message") or "" for e in self.events.list(ticket) if e.kind == "handoff" and e.payload.get("lane") == repo]
                message = squash_message(self._commit_rules(repo), ticket, pr.title.split(": ", 1)[-1], changed, messages)
                _, sha, error = await anyio.to_thread.run_sync(squash, lane, message)
                await anyio.to_thread.run_sync(self.prs.refresh, pr)
                if error:
                    return error
                return f"{repo} already had everything from {lane.base}; committed its staged change as {sha[:8]}."
            return f"{repo} already has everything from {lane.base}."
        if result.ok:
            if pr is not None:
                await anyio.to_thread.run_sync(self.prs.refresh, pr)
            self.events.append(ticket, "lane.rebased", {"lane": repo, "onto": result.onto[:12], "base": lane.base})
            passed, failed = await self._recheck(ticket, repo, lane)
            self.events.append(ticket, "lane.rechecked", {"lane": repo, "passed": passed, "failed": failed})
            return f"Rebased {repo} on {lane.base}; checks {'pass' if passed else 'fail: ' + ', '.join(failed)}."
        files = ", ".join(result.conflicts)
        if resolve_with_coder and self._open_gate(ticket, repo):
            conflicted = await anyio.to_thread.run_sync(merge_for_resolution, lane)
            note = (f"{lane.base} moved on and conflicts with this lane in: {', '.join(conflicted) or files}. "
                    "The merge is in progress in your worktree with conflict markers in those files. Resolve every marker, "
                    "keep both sides' intent, run the tests, and do not run git yourself: the orchestrator commits the merge.")
            self.events.append(ticket, "lane.conflict", {"lane": repo, "files": conflicted or result.conflicts, "sent_to": "coder"})
            await self.answer(ticket, repo, SEND_BACK, note)
            return f"Sent {repo} back to the coder to resolve conflicts in {files}."
        self.events.append(ticket, "lane.conflict", {"lane": repo, "files": result.conflicts})
        return f"{lane.base} conflicts with {repo} in {files}; nothing was changed."

    def _commit_rules(self, repo: str) -> CommitRules:
        try:
            return load_repo_config(self._workspace.repos / repo, load_pack("codec-standard"), local_dir=self.repos.local_dir).commit
        except Exception:
            return CommitRules()

    def _lane(self, ticket: str, repo: str) -> tuple[Lane, LocalPR | None]:
        loaded = self.local_pr(ticket, repo)
        if loaded is not None:
            return loaded[0].lane(), loaded[0]
        path = self._workspace.worktrees / ticket / repo
        if not (path / ".git").exists():
            raise KeyError(f"{ticket} has no lane for {repo} yet")
        stored = self.stored_request(ticket)
        base = (stored[0].bases.get(repo) if stored else None) or self.repos.configured_base(repo) or "develop"
        branch = subprocess.run(["git", "branch", "--show-current"], cwd=path, capture_output=True, text=True).stdout.strip()
        return Lane(ticket=ticket, repo=repo, path=path, branch=branch, base=base), None

    async def _recheck(self, ticket: str, repo: str, lane: Lane) -> tuple[bool, list[str]]:
        config = load_repo_config(self._workspace.repos / repo, load_pack("codec-standard"), local_dir=self.repos.local_dir)
        env = self.envs.for_lane(ticket, repo)

        def run_all() -> list[str]:
            return [c.id for c in config.checks if not run_check(c, lane.path, env)[0]]

        failed = await anyio.to_thread.run_sync(run_all)
        return not failed, failed

    async def recover(self, ticket: str) -> MissionResult:
        runtime = await self.runtime(ticket)
        async with runtime.lock:
            try:
                return await runtime.coordinator.recover(ticket)
            finally:
                self._log_jev(runtime)

    # --- local PRs ----------------------------------------------------------------

    @property
    def prs(self) -> GitHubPublisher:
        return GitHubPublisher({}, record_dir=self._workspace.record_dir)

    def local_pr(self, ticket: str, repo: str) -> tuple[LocalPR, str] | None:
        """The lane's local PR, re-pointed at its branch if the branch moved since it was written."""
        loaded = self.prs.load(ticket, repo)
        if loaded is None:
            return None
        pr, body = loaded
        if (pr.path / ".git").exists() and not self.is_busy(ticket):
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=pr.path, capture_output=True, text=True).stdout.strip()
            if head and head != pr.sha and not (pr.sha is None and head == pr.base_sha):
                pr = self.prs.refresh(pr)
        return pr, body

    def lane_state(self, pr: LocalPR) -> str | None:
        """Why a local PR has nothing to push or merge, in words, or None when it does."""
        if pr.sha is not None or not (pr.path / ".git").exists():
            return None
        staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=pr.path, capture_output=True, text=True).stdout.split()
        if staged:
            return (f"The branch has no commits on top of {pr.base}, but {len(staged)} changed file"
                    f"{'s are' if len(staged) != 1 else ' is'} staged in the worktree: an earlier squash was interrupted. "
                    "Update from base commits them as the squash, through the repo's hooks.")
        return f"The branch has no commits on top of {pr.base}: this lane changed nothing to push or merge."

    async def pr_action(self, ticket: str, repo: str, action: str, target: str) -> str:
        """Push the local PR to GitHub or merge it locally, into target; then approve its open PR or review gate.

        The gate is answered only after the push or merge worked, so a conflict leaves the lane waiting on me.
        """
        loaded = self.local_pr(ticket, repo)
        if loaded is None:
            raise KeyError(f"{ticket} has no local PR for {repo}")
        pr = loaded[0]
        target = target.strip()
        if subprocess.run(["git", "check-ref-format", "--branch", target], capture_output=True).returncode != 0:
            raise ValueError(f"{target!r} is not a branch name")
        if action == "push":
            result = await anyio.to_thread.run_sync(self.prs.open_on_github, pr, target)
            self.events.append(ticket, "pr.pushed", {"lane": repo, "url": result, "target": target})
        elif action == "merge":
            result = await anyio.to_thread.run_sync(self.prs.merge_local, pr, target)
            self.events.append(ticket, "pr.merged", {"lane": repo, "target": target, "sha": result})
        else:
            raise ValueError(f"unknown action {action}")
        if self._open_gate(ticket, repo):
            await self.answer(ticket, repo, APPROVE)
        return result

    def waiting_at_gate(self, ticket: str, lane: str) -> bool:
        """The planning thread (lane "") or a lane is waiting at a gate, per the event log."""
        view = mission_view(self.events.list(ticket)) if self.events.list(ticket) else None
        if view is None:
            return False
        return view.planning_gate is not None if not lane else bool(view.lanes.get(lane) and view.lanes[lane].gate)

    def _open_gate(self, ticket: str, repo: str) -> bool:
        lane = mission_view(self.events.list(ticket)).lanes.get(repo)
        return bool(lane and lane.gate and lane.gate.kind in ("pr", "review"))

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
