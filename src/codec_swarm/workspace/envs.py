"""Per-repo environments, managed from the Repos page.

Values live in ~/.codec-swarm/env/<repo>.env (mode 600), never in SQLite, events, handoffs or prompts.
A lane gets them two ways: written into its worktree as the repo's env file (ignored by git), and as
environment variables for its checks and its agents' sessions. A mission can override single values; those
are kept in ~/.codec-swarm/env/missions/<ticket>/<repo>.env so the mission's stored request holds only names.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
from pathlib import Path

from dotenv import dotenv_values
from pydantic import BaseModel

KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
FILE_NAME = re.compile(r"\.?[A-Za-z0-9][A-Za-z0-9._-]*")
DEFAULT_FILE = ".env"
# Values that look like production credentials; a sensitive repo shows a warning next to them.
LIVE = [
    (re.compile(r"\b(sk|rk|pk)_live_[A-Za-z0-9]{8,}"), "a live Stripe key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "an AWS access key"),
    (re.compile(r"\bxox[abp]-[A-Za-z0-9-]{10,}"), "a Slack token"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"), "a GitHub token"),
    (re.compile(r"://[^/\s]*\b(prod|production|live)\b[^/\s]*", re.I), "a production host"),
]


class MaskedVar(BaseModel):
    key: str
    mask: str
    warning: str | None = None


def mask(value: str) -> str:
    return "••••" + (value[-4:] if len(value) >= 12 else "")


def live_warning(value: str) -> str | None:
    for pattern, what in LIVE:
        if pattern.search(value):
            return f"looks like {what}"
    return None


def parse(text: str) -> dict[str, str]:
    """KEY=value lines in .env syntax (quotes, comments and `export` allowed)."""
    values = {k: v for k, v in dotenv_values(stream=io.StringIO(text)).items() if v is not None}
    bad = [k for k in values if not KEY.fullmatch(k)]
    if bad:
        raise ValueError(f"Not valid variable names: {', '.join(bad)}")
    return values


def _render(values: dict[str, str]) -> str:
    def quote(v: str) -> str:
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'

    return "".join(f"{k}={quote(v)}\n" for k, v in values.items())


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    path.chmod(0o600)


class RepoEnvs:
    def __init__(self, root: Path) -> None:
        self.dir = Path(root).expanduser() / "env"

    def _path(self, repo: str) -> Path:
        return self.dir / f"{repo}.env"

    def _override_path(self, ticket: str, repo: str) -> Path:
        return self.dir / "missions" / ticket / f"{repo}.env"

    def load(self, repo: str) -> dict[str, str]:
        path = self._path(repo)
        return {k: v for k, v in dotenv_values(path).items() if v is not None} if path.exists() else {}

    def _save(self, repo: str, values: dict[str, str]) -> None:
        if values:
            _write_private(self._path(repo), _render(values))
        else:
            self._path(repo).unlink(missing_ok=True)

    def set(self, repo: str, key: str, value: str) -> None:
        if not KEY.fullmatch(key):
            raise ValueError(f"{key!r} is not a valid variable name")
        self._save(repo, {**self.load(repo), key: value})

    def merge(self, repo: str, text: str) -> list[str]:
        """Add or replace every variable in a pasted .env; returns the names it set."""
        values = parse(text)
        self._save(repo, {**self.load(repo), **values})
        return list(values)

    def delete(self, repo: str, key: str) -> None:
        self._save(repo, {k: v for k, v in self.load(repo).items() if k != key})

    def masked(self, repo: str, sensitive: bool = False) -> list[MaskedVar]:
        return [MaskedVar(key=k, mask=mask(v), warning=live_warning(v) if sensitive else None) for k, v in self.load(repo).items()]

    # --- one mission's overrides -------------------------------------------------------

    def set_overrides(self, ticket: str, repo: str, text: str) -> list[str]:
        values = parse(text)
        if values:
            _write_private(self._override_path(ticket, repo), _render(values))
        return list(values)

    def for_lane(self, ticket: str, repo: str) -> dict[str, str]:
        """The repo's environment with this mission's overrides on top."""
        path = self._override_path(ticket, repo)
        overrides = {k: v for k, v in dotenv_values(path).items() if v is not None} if path.exists() else {}
        return {**self.load(repo), **overrides}

    def drop_overrides(self, ticket: str) -> None:
        shutil.rmtree(self.dir / "missions" / ticket, ignore_errors=True)


def write_env_file(worktree: Path, name: str, values: dict[str, str]) -> str | None:
    """Write the lane's env file and keep git from ever staging it. Returns why it was skipped, if it was."""
    if not values:
        return None
    if not FILE_NAME.fullmatch(name) or "/" in name or name in (".git", ".swarm"):
        return f"{name!r} is not a file name codec-swarm writes"
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", "--", name], cwd=worktree, capture_output=True).returncode == 0
    if tracked:
        return f"{name} is tracked in this repo, so the values go to the lane as environment variables only"
    git_dir = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=worktree, capture_output=True, text=True).stdout.strip()
    if git_dir:
        exclude = Path(git_dir) / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        current = exclude.read_text() if exclude.exists() else ""
        if f"/{name}\n" not in current:
            exclude.write_text(current + ("" if current.endswith("\n") or not current else "\n") + f"/{name}\n")
    _write_private(worktree / name, _render(values))
    return None
