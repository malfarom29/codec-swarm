"""Local PRs: a judged lane becomes one squashed commit and a description I review in the dashboard.

From there I either push it and open it on GitHub (gh CLI, your existing login) against a branch I pick,
or merge it into a local branch without pushing anything. Nothing leaves the machine until I choose.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import textwrap
from collections.abc import Callable, Sequence
from pathlib import Path

from pydantic import BaseModel

from codec_swarm.domain import Handoff, Mission
from codec_swarm.harness.config import CommitRules
from codec_swarm.workspace.lanes import SWARM_IDENTITY, Lane, _normalized, git, lane_spec_files

Runner = Callable[[Sequence[str], str], str]  # (argv, cwd) -> stdout


def run_command(argv: Sequence[str], cwd: str) -> str:
    proc = subprocess.run(list(argv), cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(argv[:3])} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


class LocalPR(BaseModel):
    """What prepare() wrote for one lane, kept as pr.json beside the mission's handoffs."""

    ticket: str
    repo: str
    title: str
    path: Path  # the lane worktree
    branch: str
    base: str  # the branch the lane started from
    base_sha: str
    sha: str | None = None  # the squashed commit; None when the lane changed nothing
    pushed_url: str | None = None
    pushed_to: str | None = None
    merged_into: str | None = None
    merged_sha: str | None = None
    squash_error: str | None = None  # why the lane kept its own commits instead of one

    def lane(self) -> Lane:
        return Lane(ticket=self.ticket, repo=self.repo, path=self.path, branch=self.branch, base=self.base)


def files_no_handoff_listed(lane: Lane, handoffs: list[Handoff]) -> list[str]:
    listed = _normalized(lane.path, [f for h in handoffs for f in h.files_touched])
    added = git(lane.path, "diff", "--name-only", "--diff-filter=A", f"origin/{lane.base}...HEAD").splitlines()
    return [p for p in added if p and not p.startswith(".swarm/") and p not in listed]


def change_description(handoffs: list[Handoff]) -> str:
    """One description of the change: the latest summary a role wrote of the whole change, else the commit messages."""
    for h in reversed(handoffs):
        if h.change_summary.strip():
            return h.change_summary.strip()
    messages = list(dict.fromkeys(h.commit_message.strip() for h in handoffs if h.commit_message.strip()))
    return "\n".join(f"- {m}" for m in messages) or "No description was written for this change."


def pr_body(mission: Mission, handoffs: list[Handoff], verdict: dict | None, lane: Lane | None = None) -> str:
    lines = [f"**{mission.ticket}**: {mission.title}", "", "## What changed", "", change_description(handoffs)]
    if mission.description:
        lines += ["", "<details><summary>Ticket</summary>", "", mission.description.strip(), "", "</details>"]
    if verdict:
        score = f"{verdict['score']:.2f}" if verdict.get("score") is not None else "no score"
        lines += ["", "## Checks", "", f"Judge: {verdict.get('source')} · {score} · {verdict.get('band')} band", ""]
        lines += [f"- {line}" for line in (verdict.get("rationale") or "").splitlines() if line.strip() and not line.startswith(("scenario ", "lane done"))]
    if mission.upstream:
        lines += ["", "## Depends on", "", "Merge these first, then point this repo's dependency back at their base branch:", ""]
        lines += [f"- `{up.repo}` on branch `{up.branch}`" for up in mission.upstream]
    unlisted = files_no_handoff_listed(lane, handoffs) if lane else []
    if unlisted:
        lines += ["", "## Files no handoff listed", "", "Check these before merging; they may be scratch files:", "", *[f"- `{p}`" for p in unlisted]]
    specs = sorted(lane_spec_files(lane.path, lane.base) or []) if lane else []
    for spec in specs:
        path = lane.path / spec
        if path.is_file():
            lines += ["", f"<details><summary>Approved spec: <code>{path.name}</code></summary>", "", "```gherkin", path.read_text().strip()[:20_000], "```", "", "</details>"]
    lines += ["", "Opened by codec-swarm."]
    return "\n".join(lines)


CONVENTIONAL = re.compile(r"^(?P<type>[a-z]+)(?:\((?P<scope>[^)]+)\))?!?:\s")


def squash_message(rules: CommitRules, ticket: str, title: str, description: str, commit_messages: list[str]) -> str:
    """The squash commit's message in the repo's format: a typed first line, a wrapped body, a footer."""
    parsed = [m for m in (CONVENTIONAL.match(c) for c in commit_messages if c) if m]
    common = lambda values: max(set(values), key=values.count) if values else ""  # noqa: E731
    kind = rules.type or common([m["type"] for m in parsed]) or "feat"
    scope = common([m["scope"] for m in parsed if m["scope"]])
    header = rules.format.format(type=kind, scope=scope, ticket=ticket, title=title.strip()).strip()
    header = header.replace("()", "")  # a {scope} slot with no scope
    if len(header) > rules.header_max:
        header = header[: rules.header_max].rsplit(" ", 1)[0].rstrip(" ,;:-")
    body = []
    for paragraph in description.strip().split("\n"):
        if not paragraph.strip():
            body.append("")
            continue
        bullet = re.match(r"^(\s*[-*]\s+)", paragraph)
        indent = " " * len(bullet.group(1)) if bullet else ""
        body.append(textwrap.fill(paragraph.strip(), rules.body_width, subsequent_indent=indent, break_long_words=False, break_on_hyphens=False))
    footer = rules.footer.format(ticket=ticket) if rules.footer else ""
    return "\n\n".join(part for part in (header, "\n".join(body).strip(), footer) if part)


def squash(lane: Lane, message: str) -> tuple[str, str | None, str | None]:
    """Fold the lane's commits into one on top of where it started. Returns (base sha, head sha or None, error).

    If the repo's hooks refuse the squash commit, the lane keeps its own commits (each passed those hooks)
    and the error says why, so a picky commit-msg rule never fails a lane that is otherwise done.
    """
    base_sha = git(lane.path, "merge-base", f"origin/{lane.base}", "HEAD")
    head = git(lane.path, "rev-parse", "HEAD")
    count = int(git(lane.path, "rev-list", "--count", f"{base_sha}..HEAD"))
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=lane.path).returncode != 0
    if count == 0 and staged:
        # No commits but the change is staged: an earlier squash moved the branch back and its commit was refused.
        # Committing what is staged is that squash; if the hooks refuse it again, the change stays staged.
        proc = subprocess.run(["git", *SWARM_IDENTITY, "commit", "--quiet", "-m", message], cwd=lane.path, capture_output=True, text=True)
        if proc.returncode != 0:
            output = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-15:])
            return base_sha, None, f"The lane's change is staged but not committed, and the repo's hooks refused the commit:\n{output}"
        return base_sha, git(lane.path, "rev-parse", "HEAD"), None
    if count == 0:
        return base_sha, None, None
    if count > 1 or git(lane.path, "log", "-1", "--format=%B", "HEAD").strip() != message.strip():
        git(lane.path, "reset", "--quiet", "--soft", base_sha)
        proc = subprocess.run(["git", *SWARM_IDENTITY, "commit", "--quiet", "--allow-empty", "-m", message], cwd=lane.path, capture_output=True, text=True)
        if proc.returncode != 0:
            git(lane.path, "reset", "--quiet", "--soft", head)  # back to the lane's own commits
            output = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-15:])
            return base_sha, head, f"The repo's hooks refused the squash commit, so the lane keeps its {count} commits:\n{output}"
    return base_sha, git(lane.path, "rev-parse", "HEAD"), None


def _worktree_on(clone: Path, branch: str) -> Path | None:
    current = None
    for line in git(clone, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            current = Path(line.removeprefix("worktree "))
        elif line == f"branch refs/heads/{branch}":
            return current
    return None


class Rebase(BaseModel):
    ok: bool
    onto: str  # the base commit the lane now sits on (or would have)
    conflicts: list[str] = []
    up_to_date: bool = False


def update_from_base(lane: Lane) -> Rebase:
    """Fetch, then replay the lane's commits on the newest origin/<base>. A conflict is backed out completely."""
    git(lane.path, "fetch", "--quiet", "origin", lane.base)
    onto = git(lane.path, "rev-parse", f"origin/{lane.base}")
    if subprocess.run(["git", "merge-base", "--is-ancestor", onto, "HEAD"], cwd=lane.path, capture_output=True).returncode == 0:
        return Rebase(ok=True, onto=onto, up_to_date=True)
    proc = subprocess.run(["git", *SWARM_IDENTITY, "rebase", "--autostash", f"origin/{lane.base}"], cwd=lane.path, capture_output=True, text=True)
    if proc.returncode == 0:
        return Rebase(ok=True, onto=onto)
    conflicts = git(lane.path, "diff", "--name-only", "--diff-filter=U").splitlines()
    subprocess.run(["git", "rebase", "--abort"], cwd=lane.path, capture_output=True)
    return Rebase(ok=False, onto=onto, conflicts=conflicts or ["(git did not name the files)"])


def merge_for_resolution(lane: Lane) -> list[str]:
    """Merge origin/<base> into the lane and leave the conflict markers for the coder; the next step's commit ends the merge."""
    proc = subprocess.run(["git", *SWARM_IDENTITY, "merge", "--no-ff", "--no-commit", f"origin/{lane.base}"], cwd=lane.path, capture_output=True, text=True)
    return git(lane.path, "diff", "--name-only", "--diff-filter=U").splitlines() if proc.returncode != 0 else []


class GitHubPublisher:
    def __init__(
        self, lanes: dict[tuple[str, str], Lane], run: Runner = run_command, record_dir: Callable[[str, str], Path] | None = None,
        commit_rules: dict[str, CommitRules] | None = None,
    ) -> None:
        self._lanes = lanes
        self._commit_rules = commit_rules or {}
        self._run = run
        self._record_dir = record_dir or (lambda ticket, repo: lanes[(ticket, repo)].path.parent / ".records" / repo)

    def push_branch(self, ticket: str, repo: str) -> str:
        """Push a judged lane's branch so dependent lanes can use it; its PR stays local until I push it."""
        lane = self._lanes[(ticket, repo)]
        git(lane.path, "push", "--quiet", "--force-with-lease", "-u", "origin", lane.branch)
        return lane.branch

    # --- the local PR ---------------------------------------------------------------

    def _files(self, ticket: str, repo: str) -> tuple[Path, Path]:
        folder = self._record_dir(ticket, repo)
        return folder / "pr.json", folder / "pr.md"

    def load(self, ticket: str, repo: str) -> tuple[LocalPR, str] | None:
        meta, body = self._files(ticket, repo)
        if not meta.exists():
            return None
        return LocalPR.model_validate_json(meta.read_text()), body.read_text() if body.exists() else ""

    def _save(self, pr: LocalPR, body: str | None = None) -> None:
        meta, md = self._files(pr.ticket, pr.repo)
        meta.parent.mkdir(parents=True, exist_ok=True)
        meta.write_text(pr.model_dump_json(indent=2))
        if body is not None:
            md.write_text(body)

    def prepare(self, mission: Mission, handoffs: list[Handoff], verdict: dict | None) -> str:
        """Squash the lane and write its description. Re-running (a rework, a crash) squashes again and rewrites both."""
        lane = self._lanes[(mission.ticket, mission.repo)]
        title = f"{mission.ticket}: {mission.title}"
        body = pr_body(mission, handoffs, verdict, lane)
        rules = self._commit_rules.get(mission.repo, CommitRules())
        message = squash_message(rules, mission.ticket, mission.title, change_description(handoffs), [h.commit_message for h in handoffs])
        base_sha, sha, error = squash(lane, message)
        pr = LocalPR(
            ticket=mission.ticket, repo=mission.repo, title=title, path=lane.path, branch=lane.branch, base=lane.base,
            base_sha=base_sha, sha=sha, squash_error=error,
        )
        self._save(pr, body)
        return f"/missions/{mission.ticket}/prs/{mission.repo}"

    def open_on_github(self, pr: LocalPR, target: str) -> str:
        """Push the squashed branch and open (or find) its GitHub PR into target. Safe to call twice."""
        loaded = self.load(pr.ticket, pr.repo)
        body = loaded[1] if loaded else pr.title
        cwd = str(pr.path)
        git(pr.path, "push", "--quiet", "--force-with-lease", "-u", "origin", pr.branch)  # the squash rewrote any earlier push
        url = self._run(["gh", "pr", "list", "--head", pr.branch, "--json", "url", "--jq", ".[0].url"], cwd)
        if not url:
            with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
                f.write(body)
            try:
                url = self._run(["gh", "pr", "create", "--base", target, "--head", pr.branch, "--title", pr.title, "--body-file", f.name], cwd)
            finally:
                Path(f.name).unlink(missing_ok=True)
        self._save(pr.model_copy(update={"pushed_url": url, "pushed_to": target}))
        return url

    def merge_local(self, pr: LocalPR, target: str) -> str:
        """Put the squashed commit on a local branch of codec-swarm's clone. Pushes nothing. Returns the new commit."""
        if pr.sha is None:
            raise ValueError("This lane changed nothing, so there is nothing to merge.")
        if pr.merged_into:
            raise ValueError(f"Already merged into {pr.merged_into}.")
        clone = Path(git(pr.path, "rev-parse", "--path-format=absolute", "--git-common-dir")).parent
        has_local = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{target}"], cwd=clone, capture_output=True).returncode == 0
        if not has_local and subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{target}"], cwd=clone, capture_output=True).returncode != 0:
            raise ValueError(f"There is no branch {target} here or on origin.")
        where = _worktree_on(clone, target)
        temporary = where is None
        if temporary:
            where = Path(tempfile.mkdtemp(prefix="codec-merge-")) / "tree"
            git(clone, "worktree", "add", "--quiet", *([str(where), target] if has_local else ["-b", target, str(where), f"origin/{target}"]))
        try:
            if git(where, "status", "--porcelain", "--untracked-files=no"):
                raise ValueError(f"{target} has uncommitted changes in {where}; commit or stash them first.")
            # One squashed commit, or the lane's own commits when the repo's hooks refused the squash.
            proc = subprocess.run(["git", *SWARM_IDENTITY, "cherry-pick", f"{pr.base_sha}..{pr.sha}"], cwd=where, capture_output=True, text=True)
            if proc.returncode != 0:
                subprocess.run(["git", "cherry-pick", "--abort"], cwd=where, capture_output=True)
                raise ValueError(f"The change does not apply cleanly on {target}: {proc.stderr.strip() or proc.stdout.strip()}")
            merged = git(where, "rev-parse", "HEAD")
        finally:
            if temporary:
                subprocess.run(["git", "worktree", "remove", "--force", str(where)], cwd=clone, capture_output=True)
        self._save(pr.model_copy(update={"merged_into": target, "merged_sha": merged}))
        return merged

    def refresh(self, pr: LocalPR) -> LocalPR:
        """The branch moved (a rebase, a hand repair): point the local PR at its head and base again."""
        lane = pr.lane()
        base_sha = git(lane.path, "merge-base", f"origin/{lane.base}", "HEAD")
        sha = git(lane.path, "rev-parse", "HEAD") if base_sha != git(lane.path, "rev-parse", "HEAD") else None
        updated = pr.model_copy(update={"base_sha": base_sha, "sha": sha})
        self._save(updated)
        return updated

    def branches(self, pr: LocalPR) -> list[str]:
        """Branches a PR could point at: origin's, then local ones, without the lane's own."""
        out = git(pr.path, "for-each-ref", "--format=%(refname)", "refs/remotes/origin", "refs/heads")
        names = [r.removeprefix("refs/remotes/origin/").removeprefix("refs/heads/") for r in out.splitlines()]
        return [n for n in dict.fromkeys(names) if n not in ("HEAD", pr.branch)]

    def diff(self, pr: LocalPR, limit: int = 200_000) -> tuple[str, str]:
        if pr.sha is None:
            return "", ""
        stat = git(pr.path, "diff", "--stat", pr.base_sha, pr.sha)
        return stat, git(pr.path, "diff", pr.base_sha, pr.sha)[:limit]

