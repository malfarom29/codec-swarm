"""Commit messages the repo's hooks accept: the squash message, the fallback, and rejections sent back to the agent."""

import subprocess

import anyio

from codec_swarm.domain import CODEC_STANDARD, Handoff, Mission
from codec_swarm.graph import MissionRunner
from codec_swarm.harness.config import BranchFlow, CommitRules
from codec_swarm.plugins.api import CommitRejected
from codec_swarm.plugins.fake import FakeBackend, FakeJudge
from codec_swarm.plugins.fallback import PackOrderRouter
from codec_swarm.store import EventLog
from codec_swarm.workspace import Workspace
from codec_swarm.workspace.github import GitHubPublisher, squash_message

LONG = ("The invoice detail now includes lineItems, createdAt and updatedAt. A new PATCH v1/shops/:shopId/invoices/:id/notes "
        "endpoint lets support correct an invoice number without changing the invoice's status.")


def test_the_squash_message_takes_the_agents_type_and_wraps_the_body():
    message = squash_message(CommitRules(), "SHOP-405", "Invoice detail endpoint", LONG,
                             ["feat(cms): add invoice detail", "test(cms): cover the patch", "feat(cms): patch data"])
    header, *rest = message.split("\n")
    assert header == "feat: Invoice detail endpoint"
    assert all(len(line) <= 72 for line in rest)
    assert message.endswith("Refs: SHOP-405")


def test_the_repo_format_and_limits_apply():
    rules = CommitRules(format="{type}({scope}): {title} [{ticket}]", header_max=60, body_width=100, footer="")
    message = squash_message(rules, "SHOP-405", "Invoice detail endpoint and a way to correct its notes", LONG, ["fix(cms): x"])
    header = message.split("\n")[0]
    assert header.startswith("fix(cms): Invoice detail endpoint") and len(header) <= 60
    assert "Refs:" not in message and all(len(line) <= 100 for line in message.split("\n"))


def _git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@l", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def test_a_refused_squash_keeps_the_lane_commits(tmp_path):
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "a.txt").write_text("a\n")
    _git(seed, "init", "-q", "-b", "develop")
    _git(seed, "add", ".")
    _git(seed, "commit", "-qm", "init")
    _git(tmp_path, "clone", "-q", "--bare", str(seed), "origin.git")
    workspace = Workspace(tmp_path / "root")
    lane = workspace.prepare_lane(str(tmp_path / "origin.git"), "app", "T-1", "t", BranchFlow(base="develop"))
    handoffs = []
    for i in (1, 2):
        (lane.path / f"f{i}.txt").write_text(f"{i}\n")
        h = Handoff(from_role="backend-coder", summary="x", commit_message=f"fix: step {i}")
        workspace.record_handoff(lane, i, h, "reviewer")
        handoffs.append(h)
    hook = lane.path.parent.parent.parent / "repos" / "app" / ".git" / "hooks" / "commit-msg"
    hook.write_text('#!/bin/sh\nhead -1 "$1" | grep -q "^fix: step" || { echo "subject must start with fix: step"; exit 1; }\n')
    hook.chmod(0o755)
    publisher = GitHubPublisher({("T-1", "app"): lane}, record_dir=workspace.record_dir)
    publisher.prepare(Mission(ticket="T-1", repo="app", title="t"), handoffs, None)
    pr, _ = publisher.load("T-1", "app")
    assert pr.squash_error and "subject must start with fix: step" in pr.squash_error
    assert _git(lane.path, "rev-list", "--count", "origin/develop..HEAD") == "2"
    merged = publisher.merge_local(pr, "develop")
    assert _git(lane.path, "rev-list", "--count", f"origin/develop..{merged}") == "2"


class RejectingRecorder:
    """The repo's hooks refuse the backend-coder's commit `times` times, then accept it."""

    def __init__(self, times: int) -> None:
        self.times = times

    def record(self, mission, step, handoff, to_role):
        if handoff.from_role == "backend-coder" and self.times:
            self.times -= 1
            raise CommitRejected("✖ subject may not be empty [subject-empty]")
        return "abc123"


def _lane_run(tmp_path, times):
    backend = FakeBackend()
    events = EventLog(tmp_path / "swarm.db")
    runner = MissionRunner(tmp_path / "swarm.db", CODEC_STANDARD, backend, PackOrderRouter(), FakeJudge(), events,
                           recorder=RejectingRecorder(times), part="lane")
    result = anyio.run(runner.start, Mission(ticket="T-1", repo="api", title="t"))
    return backend, events, result


def test_a_rejected_commit_goes_back_to_the_same_agent_with_the_hook_output(tmp_path):
    backend, events, result = _lane_run(tmp_path, times=1)
    assert backend.calls[:2] == ["backend-coder", "backend-coder"]
    retry = backend.requests[1][1]
    assert retry.from_role == "orchestrator" and "subject-empty" in retry.summary
    assert result.gate["kind"] == "pr"
    assert any(e.kind == "commit.rejected" for e in events.list("T-1"))


def test_after_two_rejections_the_handoff_gate_asks_me(tmp_path):
    backend, _, result = _lane_run(tmp_path, times=5)
    assert backend.calls.count("backend-coder") == 2
    assert result.gate["kind"] == "handoff"
