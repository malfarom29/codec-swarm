"""Jev pack choice: Solo or Codec standard from the ticket and its repos. Sensitive repos never go Solo."""

from __future__ import annotations

from typesafe_sdk import Choice, TypeSafeError

from codec_swarm.domain import SOLO, STANDARD, Decision, Mission, choose_pack_rule
from codec_swarm.plugins.jev.client import SystemOne

PACK_CRITERIA = {
    SOLO: "One coder can finish it: small, well specified, one repo, low risk.",
    STANDARD: "Needs a spec, review and hardening: ambiguous, cross-cutting, several repos or risky.",
}
MIN_CONFIDENCE = 0.70


async def choose_pack_jev(ask: SystemOne, mission: Mission, repo_count: int, sensitive: bool) -> Decision:
    rule = choose_pack_rule(bool(mission.description.strip()), repo_count, sensitive)
    if sensitive:
        return rule.model_copy(update={"rationale": "sensitive repo", "source": "rule (sensitive repo)"})
    state = {"ticket": {"title": mission.title, "description": mission.description}, "repos": repo_count}
    try:
        answer = (await ask(state, {"pack": Choice(instructions="Which pack should run this ticket?", criteria=PACK_CRITERIA)})).answers["pack"]
    except TypeSafeError:
        return rule.model_copy(update={"source": "rule (jev unavailable)"})
    options = dict(answer.probabilities)
    if options.get(answer.choice, 0.0) < MIN_CONFIDENCE:
        return rule.model_copy(update={"options": options, "rationale": f"Jev unsure; {rule.rationale}"})
    return Decision(next=answer.choice, source="jev", rationale="Jev's pick", options=options)
