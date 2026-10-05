"""Load a role pack from disk: pack.yaml, constitution layers, role prompts and stack defaults."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from codec_swarm.domain import Pack

PACKS_DIR = Path(__file__).resolve().parents[3] / "packs"


MODEL_ORDER = ("haiku", "sonnet", "opus")


class RoleSpec(BaseModel, frozen=True):
    model: str = "sonnet"
    min_model: str = "haiku"  # floor for Jev's model routing
    plans_lanes: bool = False  # this role's handoff includes the lane order
    mcp: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    max_turns: int = 40


class PackDefinition(BaseModel, frozen=True):
    root: Path
    pack: Pack
    roles: dict[str, RoleSpec]
    mcp_catalog: dict[str, dict[str, Any]]
    base: Path | None = None  # the pack this one extends; its files fill in what this pack lacks

    def _find(self, relative: str) -> Path | None:
        for root in (self.root, self.base):
            if root is not None and (root / relative).exists():
                return root / relative
        return None

    def layer(self, relative: str) -> str | None:
        path = self._find(relative)
        return path.read_text().strip() if path else None

    def role_prompt(self, role: str) -> str:
        prompt = self.layer(f"roles/{role}.md")
        if prompt is None:
            raise FileNotFoundError(f"pack {self.pack.name} has no prompt for role {role}")
        return prompt

    def stack_defaults(self, stack: str) -> dict[str, Any]:
        path = self._find(f"stacks/{stack}.yaml")
        return yaml.safe_load(path.read_text()) if path else {}


def _pack_root(name_or_path: str | Path) -> Path:
    root = Path(name_or_path)
    return root if root.is_dir() else PACKS_DIR / str(name_or_path)


def load_pack(name_or_path: str | Path) -> PackDefinition:
    root = _pack_root(name_or_path)
    raw = yaml.safe_load((root / "pack.yaml").read_text())
    base = load_pack(raw["extends"]) if raw.get("extends") else None
    pack = Pack(
        name=raw["name"],
        planning_roles=tuple(raw["planning_roles"]),
        lane_roles=tuple(raw["lane_roles"]),
        max_rework=raw.get("max_rework", 2),
    )
    roles = {name: RoleSpec(**spec) for name, spec in raw.get("roles", {}).items()}
    for role in (*pack.planning_roles, *pack.lane_roles):
        roles.setdefault(role, RoleSpec())
    catalog = {**(base.mcp_catalog if base else {}), **raw.get("mcp_catalog", {})}
    return PackDefinition(root=root, pack=pack, roles=roles, mcp_catalog=catalog, base=base.root if base else None)
