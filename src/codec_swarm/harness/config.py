"""The per-repo `.swarm/config.yaml`: only what differs from the stack default shipped in the pack."""

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


class BranchFlow(BaseModel, frozen=True):
    base: str = "develop"
    branch: str = "feature/{ticket}-{slug}"


class Report(BaseModel, frozen=True):
    kind: str  # junit | mutation
    path: str


class Check(BaseModel, frozen=True):
    id: str
    run: str
    report: Report | None = None
    min: float | None = None


class RoleExtras(BaseModel, frozen=True):
    extra_mcp: tuple[str, ...] = ()
    extra_skills: tuple[str, ...] = ()


class RepoConfig(BaseModel, frozen=True):
    version: int = 1
    stack: str
    pack: str = "codec-standard"
    sensitive: bool = False  # payments or personal data: stricter thresholds, never the Solo pack
    branch_flow: BranchFlow = BranchFlow()
    checks: tuple[Check, ...] = ()
    judge: JudgeBands = JudgeBands()
    allowlist: tuple[str, ...] = ()
    roles: dict[str, RoleExtras] = Field(default_factory=dict)


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Repo keys override stack defaults; nested mappings merge, lists replace."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_repo_config(repo: Path, pack: PackDefinition, stack: str | None = None) -> RepoConfig:
    path = repo / ".swarm" / "config.yaml"
    own = yaml.safe_load(path.read_text()) if path.exists() else {}
    stack = own.get("stack") or stack
    if not stack:
        raise ValueError(f"{repo} has no .swarm/config.yaml and no stack was given")
    return RepoConfig.model_validate(_merge(pack.stack_defaults(stack), {**own, "stack": stack}))
