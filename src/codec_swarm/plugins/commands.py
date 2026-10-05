"""Resolve a command to what it really runs, so a harmless name can't hide a dangerous script."""

from __future__ import annotations

import json
import re
from pathlib import Path

SCRIPT_RUNNER = re.compile(r"^(?:npm\s+run|npm\s+(?=test\b|start\b)|pnpm(?:\s+run)?|yarn(?:\s+run)?)\s+([\w:.-]+)(.*)$")
MAKE = re.compile(r"^make\s+([\w.-]+)(.*)$")


def _package_scripts(worktree: Path) -> dict[str, str]:
    path = worktree / "package.json"
    if not path.exists():
        return {}
    try:
        return dict(json.loads(path.read_text()).get("scripts") or {})
    except (ValueError, AttributeError):
        return {}


def _make_target(worktree: Path, target: str) -> str | None:
    path = worktree / "Makefile"
    if not path.exists():
        return None
    lines = path.read_text().splitlines()
    for i, line in enumerate(lines):
        if re.match(rf"^{re.escape(target)}\s*:", line):
            recipe = []
            for body in lines[i + 1 :]:
                if not body.startswith("\t"):
                    break
                recipe.append(body.strip().lstrip("@"))
            return " && ".join(recipe) or None
    return None


def resolve_command(worktree: Path, command: str) -> str:
    """The text that will actually run. Unknown runners and plain commands resolve to themselves."""
    command = command.strip()
    if match := SCRIPT_RUNNER.match(command):
        name, rest = match.groups()
        script = _package_scripts(worktree).get(name)
        return f"{script}{rest}" if script else command
    if match := MAKE.match(command):
        target, rest = match.groups()
        recipe = _make_target(worktree, target)
        return f"{recipe}{rest}" if recipe else command
    return command
