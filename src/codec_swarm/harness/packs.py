"""Load a role pack from disk: pack.yaml, constitution layers, role prompts and stack defaults."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from codec_swarm.domain import Pack

PACKS_DIR = Path(__file__).resolve().parents[3] / "packs"


class RoleSpec(BaseModel, frozen=True):
    model: str = "sonnet"
    mcp: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    max_turns: int = 40


class PackDefinition(BaseModel, frozen=True):
    root: Path
    pack: Pack
    roles: dict[str, RoleSpec]
    mcp_catalog: dict[str, dict[str, Any]]

    def layer(self, relative: str) -> str | None:
        path = self.root / relative
        return path.read_text().strip() if path.exists() else None

    def role_prompt(self, role: str) -> str:
        prompt = self.layer(f"roles/{role}.md")
        if prompt is None:
            raise FileNotFoundError(f"pack {self.pack.name} has no prompt for role {role}")
        return prompt

    def stack_defaults(self, stack: str) -> dict[str, Any]:
        path = self.root / "stacks" / f"{stack}.yaml"
        return yaml.safe_load(path.read_text()) if path.exists() else {}


def load_pack(name_or_path: str | Path) -> PackDefinition:
    root = Path(name_or_path)
    if not root.is_dir():
        root = PACKS_DIR / str(name_or_path)
    raw = yaml.safe_load((root / "pack.yaml").read_text())
    pack = Pack(
        name=raw["name"],
        planning_roles=tuple(raw["planning_roles"]),
        lane_roles=tuple(raw["lane_roles"]),
        max_rework=raw.get("max_rework", 2),
    )
    roles = {name: RoleSpec(**spec) for name, spec in raw.get("roles", {}).items()}
    for role in (*pack.planning_roles, *pack.lane_roles):
        roles.setdefault(role, RoleSpec())
    return PackDefinition(root=root, pack=pack, roles=roles, mcp_catalog=raw.get("mcp_catalog", {}))
