"""Repo clones and one git worktree per lane, under ~/.codec-swarm by default.

Nothing under a worktree's .swarm/ is ever committed: specs, notes and handoffs are kept per mission in
~/.codec-swarm/missions/<ticket>/<repo>/, so no branch (and no squash merge) carries them.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from pydantic import BaseModel

from codec_swarm.domain import Handoff, Mission
from codec_swarm.harness.config import BranchFlow

DEFAULT_ROOT = Path.home() / ".codec-swarm"
SWARM_IDENTITY = ["-c", "user.name=codec-swarm", "-c", "user.email=codec-swarm@localhost"]
NOT_SWARM = ("--", ".", ":(exclude).swarm")  # pathspec: the whole worktree except .swarm/
KEPT_IN_REPO = {"config.yaml", "domain.md"}  # a repo's own .swarm files, never copied into a mission record


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
        self.missions = root / "missions"

    def record_dir(self, ticket: str, repo: str) -> Path:
        """Where a lane's spec, notes and handoffs are kept, outside its branch."""
        return self.missions / ticket / repo

    def mission_dir(self, ticket: str) -> Path:
        """Holds every lane worktree of a mission; planning roles work here so they can write each repo's spec."""
        return self.worktrees / ticket

    def discard(self, ticket: str) -> list[str]:
        """Remove a mission's worktrees and their local branches, and set its record aside. Returns what it removed.

        Remote branches are left alone: a pushed PR is updated by the next push of the same branch.
        """
        removed = []
        folder = self.worktrees / ticket
        for worktree in sorted(folder.iterdir()) if folder.is_dir() else []:
            if not (worktree / ".git").exists():
                continue
            clone = self.repos / worktree.name
            branch = subprocess.run(["git", "branch", "--show-current"], cwd=worktree, capture_output=True, text=True).stdout.strip()
            subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=clone, capture_output=True)
            if branch:
                subprocess.run(["git", "branch", "-D", branch], cwd=clone, capture_output=True)
            removed.append(f"{worktree.name}:{branch}")
        shutil.rmtree(folder, ignore_errors=True)
        record = self.missions / ticket
        if record.exists():
            stamp = 1
            while (self.missions / f"{ticket}.run{stamp}").exists():
                stamp += 1
            record.rename(self.missions / f"{ticket}.run{stamp}")  # earlier runs' handoffs and specs stay readable
        return removed

    def clone(self, origin: str, name: str) -> Path:
        """Clone once, then fetch before each mission."""
        path = self.repos / name
        if path.exists():
            git(path, "fetch", "--prune", "origin")
        else:
            self.repos.mkdir(parents=True, exist_ok=True)
            git(self.repos, "clone", "--quiet", origin, name)
        exclude = path / ".git" / "info" / "exclude"  # shared by every worktree of this clone
        if exclude.parent.is_dir() and "/.swarm/" not in (exclude.read_text() if exclude.exists() else ""):
            with exclude.open("a") as f:
                f.write("\n# codec-swarm: specs and handoffs stay out of branches\n/.swarm/\n")
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
        """Commit the step's code changes with the agent's message and keep the handoff in the mission record. Returns HEAD.

        Agents never commit themselves: git commit is not on any allowlist, and one commit per step keeps history readable.
        """
        unlisted = unlisted_new_files(lane.path, handoff.files_touched)
        if git(lane.path, "status", "--porcelain", *NOT_SWARM):
            git(lane.path, "add", "--all")  # .swarm/ is ignored; a spec an older mission committed is unstaged below
            subprocess.run(["git", "reset", "--quiet", "--", ".swarm"], cwd=lane.path, capture_output=True)
            message = handoff.commit_message or f"chore: {handoff.from_role} changes for {lane.ticket}"
            git(lane.path, *SWARM_IDENTITY, "commit", "--quiet", "-m", message)
        record = self.record_dir(lane.ticket, lane.repo)
        folder = record / "handoffs"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{step:02d}-{handoff.from_role}-{to_role}.md").write_text(render_handoff(lane.ticket, handoff, to_role, unlisted))
        self.archive(lane)
        return git(lane.path, "rev-parse", "HEAD")

    def archive(self, lane: Lane) -> None:
        """Copy what the agents wrote under .swarm/ (spec, design and plan notes) into the mission record."""
        swarm = lane.path / ".swarm"
        if not swarm.is_dir():
            return
        record = self.record_dir(lane.ticket, lane.repo)
        for item in swarm.iterdir():
            if item.name in KEPT_IN_REPO or item.name == "handoffs":
                continue
            target = record / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)


def lane_spec_files(worktree: Path, base: str | None) -> set[str] | None:
    """Spec files this lane wrote or changed; None when that can't be told (not a git worktree).

    Specs are never committed now, so they show up as ignored or untracked files. Older missions committed theirs,
    and a squash merge can carry an earlier mission's spec onto the base branch: those don't count.
    """
    if base is None:
        return None

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=worktree, capture_output=True, text=True)

    committed = run("diff", "--name-only", f"origin/{base}...HEAD", "--", ".swarm/spec")
    pending = run("status", "--porcelain", "--ignored", "--untracked-files=all", "--", ".swarm/spec")
    if committed.returncode != 0 or pending.returncode != 0:
        return None
    return set(committed.stdout.split()) | {line[3:] for line in pending.stdout.splitlines()}


def _normalized(worktree: Path, paths: tuple[str, ...] | list[str]) -> set[str]:
    out = set()
    for raw in paths:
        path = Path(raw)
        if path.is_absolute():
            try:
                path = path.resolve().relative_to(worktree.resolve())
            except ValueError:
                continue
        out.add(path.as_posix().lstrip("./"))
    return out


def unlisted_new_files(worktree: Path, listed: tuple[str, ...] | list[str]) -> list[str]:
    """Untracked files outside .swarm/ that the handoff did not name: often scratch scripts left behind."""
    names = _normalized(worktree, listed)
    status = git(worktree, "status", "--porcelain", "--untracked-files=all")
    new = [line[3:] for line in status.splitlines() if line.startswith("?? ")]
    return [p for p in new if not p.startswith(".swarm/") and p not in names]


def render_handoff(ticket: str, handoff: Handoff, to_role: str, unlisted: list[str] | None = None) -> str:
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
    if unlisted:
        lines += ["## New files not listed", "", "The orchestrator committed these, but the handoff did not name them:", "", *[f"- `{p}`" for p in unlisted], ""]
    return "\n".join(lines)


LaneKey = tuple[str, str]  # (ticket, repo)


class WorkspaceRecorder:
    """HandoffRecorder that commits each handoff in its lane's worktree; planning handoffs go to every lane."""

    def __init__(self, workspace: Workspace, lanes: dict[LaneKey, Lane]) -> None:
        self._workspace = workspace
        self._lanes = lanes

    def record(self, mission: Mission, step: int, handoff: Handoff, to_role: str) -> str | None:
        if mission.repo:
            return self._workspace.record_handoff(self._lanes[(mission.ticket, mission.repo)], step, handoff, to_role)
        shas = [self._workspace.record_handoff(lane, step, handoff, to_role) for (t, _), lane in self._lanes.items() if t == mission.ticket]
        return shas[0] if shas else None
