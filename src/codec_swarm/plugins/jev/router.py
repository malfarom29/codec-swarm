"""Jev Router: cheapest model likely to succeed (never below the role's floor), and where a send-back goes."""

from __future__ import annotations

from typesafe_sdk import Choice, TypeSafeError

from codec_swarm.domain import Decision, Handoff, Mission, Pack
from codec_swarm.harness.packs import MODEL_ORDER, PackDefinition
from codec_swarm.plugins.fallback import PackOrderRouter
from codec_swarm.plugins.jev.client import SystemOne

MIN_CONFIDENCE = 0.70  # below this, the fixed rule decides
UNAVAILABLE = "rule (jev unavailable)"

MODEL_CRITERIA = {
    "haiku": "Mechanical or well-specified work with a known pattern in the repo.",
    "sonnet": "Typical feature work: some judgment, moderate ambiguity, a few files.",
    "opus": "High ambiguity, cross-cutting design, payments or security sensitive reasoning.",
}


class JevRouter:
    def __init__(self, pack: PackDefinition, ask: SystemOne) -> None:
        self._pack = pack
        self._ask = ask
        self._rule = PackOrderRouter()

    async def pick_model(self, pack: Pack, mission: Mission, role: str, incoming: Handoff | None) -> Decision:
        spec = self._pack.roles[role]
        state = {
            "role": role,
            "ticket": {"id": mission.ticket, "title": mission.title, "description": mission.description},
            "incoming_handoff": incoming.model_dump(include={"from_role", "summary", "send_back", "questions"}) if incoming else None,
        }
        question = Choice(instructions="Which model is the cheapest that is likely to do this role's step well?", criteria=MODEL_CRITERIA)
        try:
            answer = (await self._ask(state, {"model": question})).answers["model"]
        except TypeSafeError:
            return Decision(next=spec.model, source=UNAVAILABLE, rationale="fixed model per role")
        floor = MODEL_ORDER.index(spec.min_model)
        chosen = answer.choice if MODEL_ORDER.index(answer.choice) >= floor else spec.min_model
        rationale = "raised to the role's floor" if chosen != answer.choice else "cheapest likely to succeed"
        return Decision(next=chosen, source="jev", rationale=rationale, options=dict(answer.probabilities))

    async def next_role(self, pack: Pack, role: str, handoff: Handoff) -> Decision:
        rule = self._rule.next_role(pack, role, handoff)
        if role not in pack.lane_roles or not handoff.send_back:
            return rule  # forward handoffs and planning roles follow pack order; Jev only decides how far back to go
        earlier = pack.lane_roles[: pack.lane_roles.index(role)] or (role,)
        criteria = {r: f"Send the work back to the {r}." for r in earlier}
        state = {"lane_roles": list(pack.lane_roles), "from_role": role, "handoff": handoff.model_dump(include={"summary", "questions", "files_touched"})}
        question = Choice(instructions="Which earlier role should fix what this handoff reports?", criteria=criteria)
        try:
            answer = (await self._ask(state, {"next_role": question})).answers["next_role"]
        except TypeSafeError:
            return rule.model_copy(update={"source": UNAVAILABLE})
        options = dict(answer.probabilities)
        if options.get(answer.choice, 0.0) < MIN_CONFIDENCE:
            return rule.model_copy(update={"rationale": f"Jev unsure ({options.get(answer.choice, 0.0):.2f}); send back one step", "options": options})
        return Decision(next=answer.choice, source="jev", rationale="send back to the role that can fix it", options=options)
