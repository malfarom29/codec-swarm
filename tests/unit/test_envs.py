"""Managed per-repo environments: storage, the lane's env file, checks, and the rules that keep values out of prompts."""

import stat
import subprocess
from pathlib import Path

import pytest

from codec_swarm.domain import Autonomy
from codec_swarm.harness.config import Check
from codec_swarm.plugins.checks import run_check
from codec_swarm.plugins.gate import Action, GateContext, hard_rules
from codec_swarm.workspace.envs import RepoEnvs, live_warning, mask, write_env_file


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_values_are_stored_privately_and_shown_masked(tmp_path):
    envs = RepoEnvs(tmp_path)
    envs.set("api", "DATABASE_URL", "postgres://u:p@localhost:5432/app")
    envs.merge("api", 'export REDIS_URL="redis://localhost:6379"\n# comment\nSHORT=abc\n')
    path = tmp_path / "env" / "api.env"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert envs.load("api") == {"DATABASE_URL": "postgres://u:p@localhost:5432/app", "REDIS_URL": "redis://localhost:6379", "SHORT": "abc"}
    shown = {v.key: v.mask for v in envs.masked("api")}
    assert shown == {"DATABASE_URL": "••••/app", "REDIS_URL": "••••6379", "SHORT": "••••"}
    envs.delete("api", "SHORT")
    assert "SHORT" not in envs.load("api")


def test_bad_names_are_refused(tmp_path):
    with pytest.raises(ValueError):
        RepoEnvs(tmp_path).set("api", "1BAD", "x")
    with pytest.raises(ValueError):
        RepoEnvs(tmp_path).merge("api", "BAD-NAME=x\n")


def test_mission_overrides_replace_single_values(tmp_path):
    envs = RepoEnvs(tmp_path)
    envs.merge("api", "DATABASE_URL=postgres://localhost/app\nAPI_KEY=test_123\n")
    assert envs.set_overrides("T-1", "api", "DATABASE_URL=postgres://localhost/other\n") == ["DATABASE_URL"]
    assert envs.for_lane("T-1", "api") == {"DATABASE_URL": "postgres://localhost/other", "API_KEY": "test_123"}
    assert envs.for_lane("T-2", "api")["DATABASE_URL"] == "postgres://localhost/app"


@pytest.mark.parametrize(("value", "flagged"), [
    ("sk_live_51HabcdefghijKLMN", True), ("sk_test_51Habcdefghij", False),
    ("AKIAABCDEFGHIJKLMNOP", True), ("postgres://user@db.prod.internal:5432/app", True), ("postgres://localhost/app", False),
])
def test_live_looking_values_are_flagged(value, flagged):
    assert (live_warning(value) is not None) is flagged


def test_the_env_file_is_written_but_never_staged(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("x\n")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@l", "commit", "-qm", "init")
    assert write_env_file(repo, ".env.local", {"API_KEY": "test_123"}) is None
    assert 'API_KEY="test_123"' in (repo / ".env.local").read_text()
    assert stat.S_IMODE((repo / ".env.local").stat().st_mode) == 0o600
    _git(repo, "add", "--all")
    assert subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=repo, capture_output=True, text=True).stdout == ""


def test_a_tracked_env_file_is_left_alone(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("PUBLIC_DEFAULT=1\n")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@l", "commit", "-qm", "init")
    assert "tracked" in write_env_file(repo, ".env", {"API_KEY": "x"})
    assert (repo / ".env").read_text() == "PUBLIC_DEFAULT=1\n"


def test_checks_run_with_the_repo_environment(tmp_path):
    ok, _ = run_check(Check(id="env", run='test "$API_KEY" = test_123'), tmp_path, {"API_KEY": "test_123"})
    assert ok


CTX = GateContext(ticket="T", role="backend-coder", worktree=Path("/tmp/lane"), autonomy=Autonomy.AUTO,
                  env_files=(".env.local",), secret_names=frozenset({"DATABASE_URL"}))


@pytest.mark.parametrize(("tool", "tool_input", "asks"), [
    ("Bash", {"command": "cat .env"}, True),
    ("Bash", {"command": "grep KEY .env.production"}, True),
    ("Bash", {"command": "printenv"}, True),
    ("Bash", {"command": "yarn test; env"}, True),
    ("Bash", {"command": "echo ${DATABASE_URL}"}, True),
    ("Bash", {"command": "cat .env.example"}, False),
    ("Bash", {"command": "API_URL=http://localhost yarn test"}, False),
    ("Read", {"file_path": ".env.local"}, True),
    ("Read", {"file_path": "/tmp/lane/.env"}, True),
    ("Read", {"file_path": ".env.example"}, False),
    ("Grep", {"pattern": "KEY", "glob": ".env*"}, True),
])
def test_agents_cannot_print_managed_values(tool, tool_input, asks):
    decision = hard_rules(tool, tool_input, CTX)
    assert (decision is not None and decision.action is Action.ASK) is asks
