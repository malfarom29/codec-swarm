"""Pick the Router, CommandGate and Judge for a mission: Jev when it is on and has a key, else the fixed-rule fallbacks."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from codec_swarm.domain import Mission
from codec_swarm.harness.config import RepoConfig
from codec_swarm.harness.packs import PackDefinition
from codec_swarm.plugins.checks import ChecksOnlyJudge
from codec_swarm.plugins.fallback import PackOrderRouter
from codec_swarm.plugins.gate import AllowlistGate
from codec_swarm.plugins.jev.client import JevClient, SystemOne
from codec_swarm.plugins.jev.gate import JevCommandGate
from codec_swarm.plugins.jev.judge import JevJudge, LaneUnderJudgement
from codec_swarm.plugins.jev.router import JevRouter
from codec_swarm.store import GateCache


@dataclass(frozen=True)
class Plugins:
    router: Any
    gate: Any
    judge: Any
    jev: bool  # whether Jev is actually in use for this mission


def jev_available(env: dict[str, str] | None = None) -> bool:
    return bool((os.environ if env is None else env).get("TYPESAFE_API_KEY"))


def build_plugins(
    pack: PackDefinition,
    config: RepoConfig,
    lane_for: Callable[[Mission], LaneUnderJudgement],
    cache: GateCache,
    use_jev: bool,
    ask: SystemOne | None = None,
    env: dict[str, str] | None = None,
    gate_threshold: float = 0.90,
    gate_margin: float = 0.03,
) -> Plugins:
    """A missing TYPESAFE_API_KEY turns Jev off whatever the mission asked for."""
    if not (use_jev and (ask is not None or jev_available(env))):
        judge = ChecksOnlyJudge(lambda m: (lane_for(m).worktree, lane_for(m).checks))
        return Plugins(router=PackOrderRouter(), gate=AllowlistGate(config.allowlist), judge=judge, jev=False)
    ask = ask or JevClient()
    return Plugins(
        router=JevRouter(pack, ask),
        gate=JevCommandGate(ask, config.allowlist, cache, gate_threshold, gate_margin),
        judge=JevJudge(ask, lane_for),
        jev=True,
    )

