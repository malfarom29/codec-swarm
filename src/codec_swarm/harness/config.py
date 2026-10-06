"""A repo's config: the stack default shipped in the pack, then the repo's `.swarm/config.yaml`, then my local file.

The local file (`~/.codec-swarm/repos.d/<repo>.yaml`) lets me configure a repo without committing anything to it.
Each layer only holds what differs from the one below it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from codec_swarm.domain import JudgeBands
from codec_swarm.harness.packs import PackDefinition


# Read-only commands every repo allows, on top of its own allowlist.
READ_ONLY_COMMANDS = (
    "git status",
    "git diff",
    "git log",
    "git show",
    "git branch --show-current",
    "git config --list",
    "uv pip list",
    # Plain reads: the hard rules still jail their paths and block redirects.
    "echo",
    "ls",
    "cat",
    "grep",
    "head",
    "tail",
    "wc",
    "pwd",
)
# Output filters an allowlisted command may be piped into.
OUTPUT_FILTERS = ("head", "tail", "grep", "wc", "sort", "uniq")


# extra="forbid": a misindented key (say, allowlist under branch_flow) is an error, not silently ignored.
class BranchFlow(BaseModel, frozen=True, extra="forbid"):
    base: str = "develop"
    branch: str = "feature/{ticket}-{slug}"


class Report(BaseModel, frozen=True, extra="forbid"):
    kind: str  # junit | mutation
    path: str


class Check(BaseModel, frozen=True, extra="forbid"):
    id: str
    run: str
    report: Report | None = None
    min: float | None = None


class RoleExtras(BaseModel, frozen=True, extra="forbid"):
    extra_mcp: tuple[str, ...] = ()
    extra_skills: tuple[str, ...] = ()


class RepoConfig(BaseModel, frozen=True, extra="forbid"):
    version: int = 1
    stack: str
    pack: str = "codec-standard"
    sensitive: bool = False  # payments or personal data: stricter thresholds, never the Solo pack
    branch_flow: BranchFlow = BranchFlow()
    checks: tuple[Check, ...] = ()
    judge: JudgeBands = JudgeBands()
    allowlist: tuple[str, ...] = ()
    roles: dict[str, RoleExtras] = Field(default_factory=dict)
    domain: str | None = None  # business rules; from repos.d/<repo>.domain.md, else the worktree's .swarm/domain.md


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Repo keys override stack defaults; nested mappings merge, lists replace."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _yaml_file(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text()) if path.exists() else None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must hold a mapping of settings")
    return data


def local_paths(local_dir: Path, name: str) -> tuple[Path, Path]:
    """My machine-local config and domain notes for one repo."""
    return local_dir / f"{name}.yaml", local_dir / f"{name}.domain.md"


class ConfigLayers(BaseModel, frozen=True):
    stack: dict[str, Any]
    repo: dict[str, Any]
    local: dict[str, Any]


def config_layers(repo: Path, pack: PackDefinition, stack: str | None = None, local_dir: Path | None = None, local: dict[str, Any] | None = None) -> ConfigLayers:
    own = _yaml_file(repo / ".swarm" / "config.yaml")
    if local is None:
        local = _yaml_file(local_paths(local_dir, repo.name)[0]) if local_dir else {}
    stack = local.get("stack") or own.get("stack") or stack
    if not stack:
        raise ValueError(f"{repo.name} has no .swarm/config.yaml, no local config and no stack was given")
    return ConfigLayers(stack={**pack.stack_defaults(stack), "stack": stack}, repo=own, local=local)


def config_sources(layers: ConfigLayers) -> dict[str, str]:
    """Which layer set each top-level setting: local, repo or stack."""
    keys = dict.fromkeys([*layers.stack, *layers.repo, *layers.local])
    return {k: "local" if k in layers.local else "repo" if k in layers.repo else "stack" for k in keys}


def load_repo_config(
    repo: Path, pack: PackDefinition, stack: str | None = None, local_dir: Path | None = None, local: dict[str, Any] | None = None
) -> RepoConfig:
    layers = config_layers(repo, pack, stack, local_dir, local)
    merged = _merge(_merge(layers.stack, layers.repo), layers.local)
    merged["stack"] = layers.stack["stack"]
    if local_dir and "domain" not in merged:
        domain = local_paths(local_dir, repo.name)[1]
        if domain.exists() and domain.read_text().strip():
            merged["domain"] = domain.read_text().strip()
    return RepoConfig.model_validate(merged)
