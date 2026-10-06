"""Conflicts handed to the coder: the merge stays open with markers, and the next step's commit completes it."""

import subprocess

from codec_swarm.domain import Handoff
from codec_swarm.harness.config import BranchFlow
from codec_swarm.workspace import Workspace
from codec_swarm.workspace.github import merge_for_resolution, update_from_base


def _git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@l", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def test_the_coder_resolves_and_the_orchestrator_commits_the_merge(tmp_path):
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "app.ts").write_text("export const value = 1;\n")
    _git(seed, "init", "-q", "-b", "develop")
    _git(seed, "add", ".")
    _git(seed, "commit", "-qm", "init")
    _git(tmp_path, "clone", "-q", "--bare", str(seed), "origin.git")
    workspace = Workspace(tmp_path / "root")
    lane = workspace.prepare_lane(str(tmp_path / "origin.git"), "app", "T-1", "value", BranchFlow(base="develop"))
    (lane.path / "app.ts").write_text("export const value = 2;\n")
    workspace.record_handoff(lane, 1, Handoff(from_role="backend-coder", summary="2", commit_message="feat: two"), "reviewer")

    _git(seed, "remote", "add", "origin", str(tmp_path / "origin.git"))
    (seed / "app.ts").write_text("export const value = 3;\n")
    _git(seed, "commit", "-qam", "three")
    _git(seed, "push", "-q", "origin", "develop")

    result = update_from_base(lane)
    assert not result.ok and result.conflicts == ["app.ts"]
    assert merge_for_resolution(lane) == ["app.ts"]
    assert "<<<<<<<" in (lane.path / "app.ts").read_text()

    (lane.path / "app.ts").write_text("export const value = 5;\n")  # the coder keeps both intents
    workspace.record_handoff(lane, 2, Handoff(from_role="backend-coder", summary="merged", commit_message="fix: merge develop"), "reviewer")
    parents = _git(lane.path, "log", "-1", "--format=%P").split()
    assert len(parents) == 2
    assert update_from_base(lane).up_to_date
