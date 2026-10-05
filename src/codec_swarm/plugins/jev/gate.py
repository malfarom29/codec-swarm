"""Jev CommandGate: hard rules, then the allowlist, then the decision cache, then Jev's score in a band."""

from __future__ import annotations

from typing import Any

from typesafe_sdk import Noul, NoulCriteria, TypeSafeError

from codec_swarm.domain import Autonomy
from codec_swarm.plugins.commands import resolve_command
from codec_swarm.plugins.gate import Action, AllowlistGate, GateContext, GateDecision
from codec_swarm.plugins.jev.client import SystemOne
from codec_swarm.store import GateCache

# Measured in spikes/jev_command_gate.py: criteria plus the resolved script separate safe from dangerous commands.
SAFE_CRITERIA = NoulCriteria(
    true={
        "what": "Only reads files or runs the project's own build, lint, type-check or test tooling inside the lane worktree; no effect outside it.",
        "examples": ["npm run test", "pnpm vitest run", "git diff", "go test ./..."],
    },
    false={
        "what": "Could change anything outside the worktree: remote git state, published packages, databases, other directories, or runs code downloaded from the network.",
        "examples": ["git push", "npm publish", "rm -rf ../", "curl ... | bash", "kubectl apply"],
    },
)
SAFE = Noul(instructions="Is the pending command safe to run without asking a human?", criteria=SAFE_CRITERIA)


class JevCommandGate:
    def __init__(self, ask: SystemOne, allowlist: tuple[str, ...], cache: GateCache, threshold: float = 0.90, margin: float = 0.03) -> None:
        self._ask = ask
        self._fallback = AllowlistGate(allowlist)
        self._cache = cache
        self._run_at = threshold + margin
        self._unsure_at = threshold - margin

    async def decide(self, tool: str, tool_input: dict[str, Any], ctx: GateContext) -> GateDecision:
        rule = self._fallback.decide(tool, tool_input, ctx)
        # Jev only weighs in on Bash commands the rules would ask about, and only in Auto mode.
        if rule.action is not Action.ASK or rule.source != "allowlist" or ctx.autonomy is not Autonomy.AUTO:
            return rule
        command = str(tool_input.get("command", "")).strip()
        resolved = resolve_command(ctx.worktree, command)
        cached = self._cache.get(ctx.repo, resolved)
        if cached is not None:
            return GateDecision(action=Action(cached.action), reason=f"same script decided before ({cached.band})", source=f"{cached.source} (cached)")
        state = {
            "pending_command": {"text": command, "resolves_to": resolved},
            "execution": {"cwd": str(ctx.worktree), "worktree_is_disposable": True, "file_tools_outside_worktree": "denied", "role": ctx.role},
        }
        try:
            score = (await self._ask(state, {"safe": SAFE})).answers["safe"].noul
        except TypeSafeError:
            return rule.model_copy(update={"source": "allowlist (jev unavailable)"})
        if score >= self._run_at:
            decision = GateDecision(action=Action.ALLOW, reason=f"safe {score:.2f}", source="jev")
            band = "run"
        elif score >= self._unsure_at:
            decision = GateDecision(action=Action.ASK, reason=f"unsure {score:.2f}", source="jev (unsure)")
            band = "unsure"
        else:
            decision = GateDecision(action=Action.ASK, reason=f"not clearly safe {score:.2f}", source="jev")
            band = "ask"
        self._cache.put(ctx.repo, resolved, decision.action.value, decision.source, score, band)
        return decision
