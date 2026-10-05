"""Command gate rules that hold with or without Jev, plus the allowlist gate used when Jev is off."""

from __future__ import annotations

import re
import shlex
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from codec_swarm.domain import Autonomy
from codec_swarm.harness.config import OUTPUT_FILTERS, READ_ONLY_COMMANDS
from codec_swarm.plugins.commands import resolve_command


class Action(StrEnum):
    ALLOW = "allow"
    ASK = "ask"  # becomes an approval in the Inbox
    DENY = "deny"


class GateContext(BaseModel, frozen=True):
    ticket: str
    repo: str = ""
    role: str
    worktree: Path
    autonomy: Autonomy


class GateDecision(BaseModel, frozen=True):
    action: Action
    reason: str
    source: str  # always-ask | jail | allowlist | rule | jev


FILE_TOOLS = {"Read": "file_path", "Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
SEARCH_TOOLS = {"Glob": "path", "Grep": "path", "LS": "path"}
HARMLESS_TOOLS = {"TodoWrite", "Skill", "WebSearch", "WebFetch", "BashOutput", "KillShell"}

# Hardcoded: no plugin, allowlist or Jev score can let these run without a human.
ALWAYS_ASK = [
    (re.compile(r"\bgit\s+push\b"), "git push"),
    (re.compile(r"\b(npm|pnpm|yarn)\s+publish\b"), "publishes a package"),
    (re.compile(r"\b(kubectl|helm)\b"), "touches a cluster"),
    (re.compile(r"\bterraform\s+(apply|destroy)\b"), "changes infrastructure"),
    (re.compile(r"\b(migrate|migration:run|db\s+push|alembic\s+upgrade)\b"), "database migration"),
    # Code passed inline can't be judged from the command line, so neither the allowlist nor Jev may approve it.
    (re.compile(r"<<|\b(python3?|node|ruby|perl)\s+-[ce]\b|\b(ba|z)?sh\s+-c\b|\beval\b"), "runs inline code"),
]
SHELL_CONTROL = re.compile(r"&&|\|\||[;|`<>]|\$\(")
PIPE = re.compile(r"(?<!\|)\|(?!\|)")
FILTER = re.compile(rf"^({'|'.join(OUTPUT_FILTERS)})(\s+[^|;&<>`$]*)?$")


def _inside(worktree: Path, raw: str) -> bool:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = worktree / path
    return path.resolve().is_relative_to(worktree.resolve())


def _bash_leaves_worktree(worktree: Path, command: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return True  # unparseable: treat as unsafe
    for token in tokens:
        if token.startswith(("/", "~", "..")) or "/.." in token:
            if not _inside(worktree, token):
                return True
    return False


def hard_rules(tool: str, tool_input: dict[str, Any], ctx: GateContext) -> GateDecision | None:
    """Rules checked before any gate plugin. Returns None when they have nothing to say."""
    if tool in FILE_TOOLS or tool in SEARCH_TOOLS:
        raw = tool_input.get(FILE_TOOLS.get(tool) or SEARCH_TOOLS[tool])
        if raw and not _inside(ctx.worktree, str(raw)):
            return GateDecision(action=Action.DENY, reason=f"{raw} is outside the lane worktree", source="jail")
        return GateDecision(action=Action.ALLOW, reason="inside the lane worktree", source="jail")
    if tool == "Bash":
        command = str(tool_input.get("command", ""))
        for pattern, why in ALWAYS_ASK:
            if pattern.search(command):
                return GateDecision(action=Action.ASK, reason=why, source="always-ask")
        if _bash_leaves_worktree(ctx.worktree, command):
            return GateDecision(action=Action.ASK, reason="touches a path outside the lane worktree", source="always-ask")
    return None


class AllowlistGate:
    """Fallback CommandGate: only allowlisted commands run without asking, and never in Manual mode."""

    def __init__(self, allowlist: tuple[str, ...]) -> None:
        self._allowlist = READ_ONLY_COMMANDS + tuple(a.strip() for a in allowlist if a.strip())

    def _allowlisted(self, command: str) -> bool:
        """An allowlisted command, optionally with stderr merged and its output piped into read-only filters."""
        head, *filters = [part.strip() for part in PIPE.split(command)]
        head = re.sub(r"\s*2>&1$", "", head)
        if SHELL_CONTROL.search(head) or not all(FILTER.match(f) for f in filters):
            return False  # a chained, redirected or piped-into-anything-else command is never covered by its first part
        return any(head == entry or head.startswith(entry + " ") for entry in self._allowlist)

    def decide(self, tool: str, tool_input: dict[str, Any], ctx: GateContext) -> GateDecision:
        hard = hard_rules(tool, tool_input, ctx)
        if hard is not None:
            return hard
        if tool == "Bash":
            # A harmless name can hide a dangerous script, so the hard rules also see what it really runs.
            command = str(tool_input.get("command", ""))
            resolved = resolve_command(ctx.worktree, command)
            if resolved != command:
                hidden = hard_rules("Bash", {"command": resolved}, ctx)
                if hidden is not None and hidden.action is not Action.ALLOW:
                    return hidden.model_copy(update={"reason": f"{hidden.reason} (inside: {resolved})"})
        if tool in HARMLESS_TOOLS:
            return GateDecision(action=Action.ALLOW, reason=f"{tool} has no side effects outside the session", source="rule")
        if tool.startswith("mcp__"):
            return GateDecision(action=Action.ALLOW, reason="MCP server loaded by the role's harness", source="harness")
        if tool != "Bash":
            return GateDecision(action=Action.ASK, reason=f"{tool} is not covered by a rule", source="rule")
        command = str(tool_input.get("command", "")).strip()
        if ctx.autonomy is not Autonomy.MANUAL and self._allowlisted(command):
            return GateDecision(action=Action.ALLOW, reason="on the repo allowlist", source="allowlist")
        return GateDecision(action=Action.ASK, reason="not on the repo allowlist", source="allowlist")
