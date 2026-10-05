"""Resolve one role's session: pack defaults, then stack, repo config, mission extras and local overrides.

Later sources only add to earlier ones; nothing silently removes a capability or a repo hook.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from codec_swarm.domain import Mission
from codec_swarm.harness.config import OUTPUT_FILTERS, READ_ONLY_COMMANDS, RepoConfig
from codec_swarm.harness.packs import PackDefinition

LAYER_SEPARATOR = "\n\n---\n\n"
ENV_REF = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}")


class MissionExtras(BaseModel, frozen=True):
    """Capabilities chosen for one mission in "New mission"."""

    mcp: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()


class LocalOverrides(BaseModel, frozen=True):
    """Machine-local tweaks from the Harness screen. They change settings, never remove capabilities."""

    model: str | None = None
    max_turns: int | None = None


class SessionSpec(BaseModel, frozen=True):
    role: str
    cwd: Path
    model: str
    system_prompt: str
    mcp_servers: dict[str, dict[str, Any]]  # secrets still as ${env:NAME}
    skills: tuple[str, ...]
    max_turns: int
    allowed_tools: tuple[str, ...] = ()  # always empty: a listed tool would skip the command gate (M0)
    setting_sources: tuple[str, ...] = ("project",)  # the repo's own .claude/ settings and hooks still load


def _unique(*groups: tuple[str, ...]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for group in groups:
        seen.update(dict.fromkeys(group))
    return tuple(seen)


def commands_section(repo: RepoConfig) -> str:
    commands = (*READ_ONLY_COMMANDS, *repo.allowlist)
    return "\n".join([
        "# Commands you can run without asking",
        "",
        *(f"- `{c}`" for c in commands),
        "",
        f"You may add `2>&1` and pipe their output into {', '.join(f'`{f}`' for f in OUTPUT_FILTERS)}.",
        "Any other command is blocked. Do not work around a blocked command: list it in your handoff questions.",
    ])


def mission_layer(mission: Mission, scenarios: str | None = None) -> str:
    lines = ["# Layer 5 · Mission", f"Ticket {mission.ticket} on {mission.repo}: {mission.title or 'untitled'}."]
    if mission.description:
        lines += ["", mission.description.strip()]
    if scenarios:
        lines += ["", "Approved scenarios:", "", scenarios.strip()]
    return "\n".join(lines)


def resolve_session(
    pack: PackDefinition,
    repo: RepoConfig,
    role: str,
    worktree: Path,
    mission: Mission,
    extras: MissionExtras = MissionExtras(),
    overrides: LocalOverrides = LocalOverrides(),
    scenarios: str | None = None,
) -> SessionSpec:
    spec = pack.roles[role]
    role_extras = repo.roles.get(role)
    mcp_names = _unique(spec.mcp, role_extras.extra_mcp if role_extras else (), extras.mcp)
    unknown = [n for n in mcp_names if n not in pack.mcp_catalog]
    if unknown:
        raise ValueError(f"MCP servers not in pack {pack.pack.name}'s catalog: {', '.join(unknown)}")

    domain = (worktree / ".swarm" / "domain.md").read_text().strip() if (worktree / ".swarm" / "domain.md").exists() else None
    layers = [
        pack.layer("constitution/01-core.md"),
        domain,
        pack.layer(f"constitution/stacks/{repo.stack}.md"),
        pack.role_prompt(role),
        mission_layer(mission, scenarios),
        commands_section(repo),
    ]
    return SessionSpec(
        role=role,
        cwd=worktree,
        model=overrides.model or spec.model,
        system_prompt=LAYER_SEPARATOR.join(layer for layer in layers if layer),
        mcp_servers={name: pack.mcp_catalog[name] for name in mcp_names},
        skills=_unique(spec.skills, role_extras.extra_skills if role_extras else (), extras.skills),
        max_turns=overrides.max_turns or spec.max_turns,
    )


def resolve_env_refs(value: Any, env: dict[str, str] | None = None) -> Any:
    """Swap ${env:NAME} for its value. Called only when a session starts, so secrets never reach logs or the UI."""
    env = dict(os.environ) if env is None else env
    if isinstance(value, str):

        def sub(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in env:
                raise KeyError(f"environment variable {name} is not set")
            return env[name]

        return ENV_REF.sub(sub, value)
    if isinstance(value, dict):
        return {k: resolve_env_refs(v, env) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [resolve_env_refs(v, env) for v in value]
    return value
