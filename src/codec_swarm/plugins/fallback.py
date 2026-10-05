"""Deterministic plugins used when Jev is off."""

from __future__ import annotations

from codec_swarm.domain import Decision, Handoff, Mission, Pack


class PackOrderRouter:
    """Forward in pack order; a send-back goes one step only, like SwarmForge's back-one."""

    def pick_model(self, pack: Pack, mission: Mission, role: str, incoming: Handoff | None) -> Decision:
        return Decision(next="", source="rule", rationale="fixed model per role from the harness")

    def next_role(self, pack: Pack, role: str, handoff: Handoff) -> Decision:
        if role in pack.planning_roles:
            return Decision(next=pack.next_role(role) or "spec_gate", source="rule", rationale="pack order")
        if handoff.send_back:
            back = pack.previous_lane_role(role) or role
            return Decision(next=back, source="rule", rationale="send back one step")
        return Decision(next=pack.next_role(role) or "judge", source="rule", rationale="pack order")
