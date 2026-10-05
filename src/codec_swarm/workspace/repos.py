"""The repos I work on: their clones, and the config I keep for them on this machine instead of in the repo."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml
from pydantic import BaseModel

from codec_swarm.harness.config import RepoConfig, config_layers, config_sources, load_repo_config, local_paths
from codec_swarm.harness.packs import PackDefinition
from codec_swarm.workspace.lanes import Workspace

REPO_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
STARTER = """# Kept on this machine only; nothing here is committed to the repo.
stack: {stack}
branch_flow:
  base: develop
  branch: "feature/{{ticket}}-{{slug}}"
# checks:
#   - id: unit
#     run: uv run pytest -q
# allowlist:
#   - uv run pytest
"""


def repo_name(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")


def check_name(name: str) -> str:
    if not REPO_NAME.fullmatch(name):
        raise ValueError(f"{name!r} is not a repo name")
    return name


class RepoSummary(BaseModel):
    name: str
    origin: str = ""
    cloned: bool = False
    repo_file: bool = False
    local_file: bool = False
    stack: str | None = None
    error: str | None = None


class RepoDetail(RepoSummary):
    config: RepoConfig | None = None
    sources: dict[str, str] = {}
    repo_text: str = ""
    local_text: str = ""
    domain_text: str = ""
    repo_domain: bool = False


class RepoCatalog:
    def __init__(self, root: Path, workspace: Workspace, pack: PackDefinition) -> None:
        self.local_dir = root / "repos.d"
        self._workspace = workspace
        self._pack = pack

    def _clone(self, name: str) -> Path:
        return self._workspace.repos / check_name(name)

    def names(self) -> list[str]:
        cloned = {p.name for p in self._workspace.repos.glob("*") if (p / ".git").exists()} if self._workspace.repos.exists() else set()
        local = {p.stem for p in self.local_dir.glob("*.yaml")} if self.local_dir.exists() else set()
        return sorted(n for n in cloned | local if REPO_NAME.fullmatch(n))

    def summary(self, name: str) -> RepoSummary:
        return self.detail(name)

    def detail(self, name: str) -> RepoDetail:
        clone = self._clone(name)
        local_yaml, local_domain = local_paths(self.local_dir, name)
        repo_yaml = clone / ".swarm" / "config.yaml"
        detail = RepoDetail(
            name=name, cloned=(clone / ".git").exists(), repo_file=repo_yaml.exists(), local_file=local_yaml.exists(),
            repo_text=repo_yaml.read_text() if repo_yaml.exists() else "", local_text=local_yaml.read_text() if local_yaml.exists() else "",
            domain_text=local_domain.read_text() if local_domain.exists() else "", repo_domain=(clone / ".swarm" / "domain.md").exists(),
        )
        if detail.cloned:
            proc = subprocess.run(["git", "remote", "get-url", "origin"], cwd=clone, capture_output=True, text=True)
            detail.origin = proc.stdout.strip()
        try:
            detail.config = load_repo_config(clone, self._pack, local_dir=self.local_dir)
            detail.sources = config_sources(config_layers(clone, self._pack, local_dir=self.local_dir))
            detail.stack = detail.config.stack
        except Exception as error:  # a broken file is shown on the page, not raised
            detail.error = str(error)
        return detail

    def add(self, url: str) -> str:
        name = check_name(repo_name(url.strip()))
        self._workspace.clone(url.strip(), name)
        return name

    def stacks(self) -> list[str]:
        return self._pack.stacks()

    def starter(self, stack: str = "python") -> str:
        return STARTER.format(stack=stack)

    def save(self, name: str, config_text: str, domain_text: str) -> None:
        """Write my local config and domain notes for a repo; empty text removes the file. Refuses a config that doesn't load."""
        clone = self._clone(name)
        local_yaml, local_domain = local_paths(self.local_dir, name)
        try:
            local = yaml.safe_load(config_text) if config_text.strip() else {}
        except yaml.YAMLError as error:
            raise ValueError(f"That is not valid YAML: {error}") from None
        if not isinstance(local, dict):
            raise ValueError("The config must be a mapping of settings, like `stack: python`.")
        stack = local.get("stack")
        if stack and stack not in self._pack.stacks():
            raise ValueError(f"Unknown stack {stack!r}; the pack has {', '.join(self._pack.stacks())}.")
        load_repo_config(clone, self._pack, local=local)  # raises with pydantic's message when a setting is wrong
        self.local_dir.mkdir(parents=True, exist_ok=True)
        for path, text in ((local_yaml, config_text), (local_domain, domain_text)):
            if text.strip():
                path.write_text(text.strip() + "\n")
            elif path.exists():
                path.unlink()
