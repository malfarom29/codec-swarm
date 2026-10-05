"""Push an approved lane and open its PR with the gh CLI (your existing login)."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence

from codec_swarm.domain import Handoff, Mission
from codec_swarm.workspace.lanes import Lane, _normalized, git, lane_spec_files

Runner = Callable[[Sequence[str], str], str]  # (argv, cwd) -> stdout


def run_command(argv: Sequence[str], cwd: str) -> str:
    proc = subprocess.run(list(argv), cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(argv[:3])} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def files_no_handoff_listed(lane: Lane, handoffs: list[Handoff]) -> list[str]:
    listed = _normalized(lane.path, [f for h in handoffs for f in h.files_touched])
    added = git(lane.path, "diff", "--name-only", "--diff-filter=A", f"origin/{lane.base}...HEAD").splitlines()
    return [p for p in added if p and not p.startswith(".swarm/") and p not in listed]


def pr_body(mission: Mission, handoffs: list[Handoff], verdict: dict | None, lane: Lane | None = None) -> str:
    lines = [f"Mission **{mission.ticket}**: {mission.title}", ""]
    if mission.description:
        lines += [mission.description.strip(), ""]
    lines += ["## Handoffs", ""]
    lines += [f"- **{h.from_role}**{' (sent back)' if h.send_back else ''}: {h.summary.splitlines()[0] if h.summary else ''}" for h in handoffs]
    if verdict:
        lines += ["", "## Judge", "", f"Source: {verdict.get('source')} · band: {verdict.get('band')}", "", "```", verdict.get("rationale", ""), "```"]
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
    lines += ["", f"The spec and handoffs stay out of this branch, in `~/.codec-swarm/missions/{mission.ticket}/{mission.repo}/`.", "", "Opened by codec-swarm."]
    return "\n".join(lines)


class GitHubPublisher:
    def __init__(self, lanes: dict[tuple[str, str], Lane], run: Runner = run_command) -> None:
        self._lanes = lanes
        self._run = run

    def push_branch(self, ticket: str, repo: str) -> str:
        """Push a judged lane's branch so dependent lanes can use it; the PR opens later as usual."""
        lane = self._lanes[(ticket, repo)]
        git(lane.path, "push", "--quiet", "-u", "origin", lane.branch)
        return lane.branch

    def publish(self, mission: Mission, handoffs: list[Handoff], verdict: dict | None) -> str:
        """Idempotent: re-running after a crash pushes again and returns the PR that already exists."""
        lane = self._lanes[(mission.ticket, mission.repo)]
        cwd = str(lane.path)
        git(lane.path, "push", "--quiet", "-u", "origin", lane.branch)
        existing = self._run(["gh", "pr", "list", "--head", lane.branch, "--json", "url", "--jq", ".[0].url"], cwd)
        if existing:
            return existing
        return self._run(
            ["gh", "pr", "create", "--base", lane.base, "--head", lane.branch, "--title", f"{mission.ticket}: {mission.title}", "--body", pr_body(mission, handoffs, verdict, lane)],
            cwd,
        )
