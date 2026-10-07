"""Resolve one role's session: pack defaults, then stack, repo config, mission extras and local overrides.

Later sources only add to earlier ones; nothing silently removes a capability or a repo hook.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from codec_swarm.domain import Mission
from codec_swarm.harness.config import OUTPUT_FILTERS, READ_ONLY_COMMANDS, RepoConfig
from codec_swarm.harness.packs import PackDefinition
from codec_swarm.harness.spec import ticket_criteria

LAYER_SEPARATOR = "\n\n---\n\n"
ENV_REF = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}")


class MissionExtras(BaseModel, frozen=True):
    """Capabilities chosen for one mission in "New mission"."""

    mcp: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()


class LocalOverrides(BaseModel, frozen=True):
    """Machine-local tweaks from the Harness screen. They change settings, never remove capabilities."""

    model: str | None = None  # forced for every step, beating the router's pick (the CLI's --model)
    default_model: str | None = None  # replaces the pack's model, used whenever the router has no pick (Jev off)
    max_turns: int | None = None
    extra_mcp: tuple[str, ...] = ()  # added to the role's servers, never replacing them
    extra_skills: tuple[str, ...] = ()


class SessionSpec(BaseModel, frozen=True):
    role: str
    cwd: Path
    model: str
    model_forced: bool = False  # a local override beats the router's pick
    plans_lanes: bool = False  # the handoff schema asks for the lane order
    system_prompt: str
    mcp_servers: dict[str, dict[str, Any]]  # secrets still as ${env:NAME}
    skills: tuple[str, ...]
    max_turns: int
    allowed_tools: tuple[str, ...] = ()  # always empty: a listed tool would skip the command gate (M0)
    setting_sources: tuple[str, ...] = ("project",)  # the repo's own .claude/ settings and hooks still load
    # The repo's managed environment: passed to the session, never dumped, logged or put in a prompt.
    env: dict[str, str] = Field(default_factory=dict, exclude=True, repr=False)
    env_file: str = ".env"


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


def commit_section(repo: RepoConfig) -> str:
    rules = repo.commit
    lines = [
        "# Commit messages",
        "",
        f"Write commit_message in Conventional Commits (type(scope): subject). Keep the first line under {rules.header_max} characters",
        f"and wrap the body at {rules.body_width}. The repo's git hooks check every commit; one they reject comes back to you.",
    ]
    if rules.rules.strip():
        lines += ["", rules.rules.strip()]
    return "\n".join(lines)


def mission_layer(mission: Mission, scenarios: str | None = None, language: str | None = None) -> str:
    lines = ["# Layer 5 · Mission", f"Ticket {mission.ticket} on {mission.repo}: {mission.title or 'untitled'}."]
    if mission.description:
        lines += ["", mission.description.strip()]
    criteria = ticket_criteria(mission.description)
    if criteria:
        lines += [
            "",
            "Acceptance criteria from the ticket. Every one must be covered by at least one scenario: tag each scenario",
            "with the criteria it covers (@AC-1 @AC-3; one scenario may cover several). If a criterion can't be written as",
            "a scenario, say why in your handoff questions.",
            "",
            *(f"- AC-{i}: {text}" for i, text in enumerate(criteria, 1)),
        ]
    if language:
        lines += ["", f"Write the Gherkin in the {language!r} dialect: the first line of every .feature file is `# language: {language}`."]
    else:
        lines += ["", "Write the Gherkin in the ticket's language. If that isn't English, start every .feature file with `# language: <code>` (es, pt, fr…)."]
    if not mission.repo and mission.repos:
        lines += [
            "",
            f"This mission spans {len(mission.repos)} repos: {', '.join(mission.repos)}.",
            "Your working directory holds one folder per repo, each that repo's lane worktree.",
            "Write each repo's scenarios to <repo>/.swarm/spec/<name>.feature.",
        ]
    for up in mission.upstream:
        lines += [
            "",
            f"This lane depends on the {up.repo} lane. Its work is on branch {up.branch}, pushed to origin but not merged.",
            f"While you work, point this repo's dependency on {up.repo} at that branch (for a uv project, the branch in",
            f"[tool.uv.sources]) and say so in your handoff, so a human repoints it once the {up.repo} PR is merged.",
        ]
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
    mcp_names = _unique(spec.mcp, role_extras.extra_mcp if role_extras else (), extras.mcp, overrides.extra_mcp)
    unknown = [n for n in mcp_names if n not in pack.mcp_catalog]
    if unknown:
        raise ValueError(f"MCP servers not in pack {pack.pack.name}'s catalog: {', '.join(unknown)}")

    domain = repo.domain or ((worktree / ".swarm" / "domain.md").read_text().strip() if (worktree / ".swarm" / "domain.md").exists() else None)
    layers = [
        pack.layer("constitution/01-core.md"),
        domain,
        pack.layer(f"constitution/stacks/{repo.stack}.md"),
        pack.role_prompt(role),
        mission_layer(mission, scenarios, repo.language),
        commands_section(repo),
        commit_section(repo),
    ]
    return SessionSpec(
        role=role,
        cwd=worktree,
        model=overrides.model or overrides.default_model or spec.model,
        model_forced=overrides.model is not None,
        plans_lanes=spec.plans_lanes,
        system_prompt=LAYER_SEPARATOR.join(layer for layer in layers if layer),
        mcp_servers={name: pack.mcp_catalog[name] for name in mcp_names},
        skills=_unique(spec.skills, role_extras.extra_skills if role_extras else (), extras.skills, overrides.extra_skills),
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
