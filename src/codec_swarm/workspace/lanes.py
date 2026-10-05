"""Repo clones and one git worktree per lane, under ~/.codec-swarm by default."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from pydantic import BaseModel

from codec_swarm.domain import Handoff, Mission
from codec_swarm.harness.config import BranchFlow

DEFAULT_ROOT = Path.home() / ".codec-swarm"
SWARM_IDENTITY = ["-c", "user.name=codec-swarm", "-c", "user.email=codec-swarm@localhost"]


def slugify(title: str, limit: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:limit].rstrip("-")


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {cwd}: {proc.stderr.strip()}")
    return proc.stdout.strip()


class Lane(BaseModel, frozen=True):
    ticket: str
    repo: str
    path: Path
    branch: str
    base: str


class Workspace:
    def __init__(self, root: Path = DEFAULT_ROOT) -> None:
        self.repos = root / "repos"
        self.worktrees = root / "worktrees"

    def clone(self, origin: str, name: str) -> Path:
        """Clone once, then fetch before each mission."""
        path = self.repos / name
        if path.exists():
            git(path, "fetch", "--prune", "origin")
        else:
            self.repos.mkdir(parents=True, exist_ok=True)
            git(self.repos, "clone", "--quiet", origin, name)
        return path

    def prepare_lane(self, origin: str, repo: str, ticket: str, title: str, flow: BranchFlow) -> Lane:
        clone = self.clone(origin, repo)
        branch = flow.branch.format(ticket=ticket, slug=slugify(title))
        path = self.worktrees / ticket / repo
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            git(clone, "worktree", "add", "--quiet", "-b", branch, str(path), f"origin/{flow.base}")
        return Lane(ticket=ticket, repo=repo, path=path, branch=branch, base=flow.base)

    def record_handoff(self, lane: Lane, step: int, handoff: Handoff, to_role: str) -> str:
        """Commit the step's code changes with the agent's message, then the handoff file. Returns the handoff commit sha.

        Agents never commit themselves: git commit is not on any allowlist, and one commit per step keeps history readable.
        """
        if git(lane.path, "status", "--porcelain"):
            git(lane.path, "add", "--all")
            message = handoff.commit_message or f"chore: {handoff.from_role} changes for {lane.ticket}"
            git(lane.path, *SWARM_IDENTITY, "commit", "--quiet", "-m", message)
        folder = lane.path / ".swarm" / "handoffs"
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{step:02d}-{handoff.from_role}-{to_role}.md"
        (folder / name).write_text(render_handoff(lane.ticket, handoff, to_role))
        git(lane.path, "add", str(folder / name))
        git(lane.path, *SWARM_IDENTITY, "commit", "--quiet", "-m", f"chore(swarm): {handoff.from_role} handoff")
        return git(lane.path, "rev-parse", "HEAD")


def render_handoff(ticket: str, handoff: Handoff, to_role: str) -> str:
    lines = [
        f"# Handoff: {handoff.from_role} → {to_role}",
        "",
        f"- Ticket: {ticket}",
        f"- Sends back: {'yes' if handoff.send_back else 'no'}",
        "",
        "## Summary",
        "",
        handoff.summary,
        "",
        "## Files touched",
        "",
        *([f"- `{f}`" for f in handoff.files_touched] or ["- none"]),
        "",
        "## Questions",
        "",
        *([f"- {q}" for q in handoff.questions] or ["- none"]),
        "",
    ]
    return "\n".join(lines)


class WorkspaceRecorder:
    """HandoffRecorder that commits each handoff in its mission's lane worktree."""

    def __init__(self, workspace: Workspace, lanes: dict[str, Lane]) -> None:
        self._workspace = workspace
        self._lanes = lanes  # ticket -> lane; one lane per mission until M3

    def record(self, mission: Mission, step: int, handoff: Handoff, to_role: str) -> str | None:
        return self._workspace.record_handoff(self._lanes[mission.ticket], step, handoff, to_role)
