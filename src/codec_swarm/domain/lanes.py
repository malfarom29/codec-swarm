"""Lane dependency order: a client lane waits until the lane it depends on reaches the judge."""

from __future__ import annotations

from codec_swarm.domain.models import LaneOrder


class LaneCycle(ValueError):
    """The lane order has a cycle, so no lane could ever start."""


def lane_dependencies(repos: tuple[str, ...], order: tuple[LaneOrder, ...]) -> dict[str, frozenset[str]]:
    """Each repo's prerequisites. Repos outside the mission are ignored; a cycle raises LaneCycle."""
    deps = {repo: frozenset() for repo in repos}
    for entry in order:
        if entry.repo in deps:
            deps[entry.repo] = frozenset(a for a in entry.after if a in deps and a != entry.repo)
    visiting, done = set(), set()

    def visit(repo: str, path: tuple[str, ...]) -> None:
        if repo in done:
            return
        if repo in visiting:
            raise LaneCycle(" -> ".join((*path, repo)))
        visiting.add(repo)
        for dep in deps[repo]:
            visit(dep, (*path, repo))
        visiting.discard(repo)
        done.add(repo)

    for repo in repos:
        visit(repo, ())
    return deps
