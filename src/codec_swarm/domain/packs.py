"""Which pack a mission runs with, when Jev is off: the plan's fixed rule."""

from __future__ import annotations

from codec_swarm.domain.models import Decision

SOLO = "solo"
STANDARD = "codec-standard"


def choose_pack_rule(has_acceptance_criteria: bool, repo_count: int, sensitive: bool) -> Decision:
    """Solo for one non-sensitive repo with clear acceptance criteria; Codec standard otherwise."""
    if sensitive:
        return Decision(next=STANDARD, source="rule", rationale="sensitive repo")
    if repo_count > 1:
        return Decision(next=STANDARD, source="rule", rationale="more than one repo")
    if not has_acceptance_criteria:
        return Decision(next=STANDARD, source="rule", rationale="ticket needs a spec")
    return Decision(next=SOLO, source="rule", rationale="one repo, clear acceptance criteria")
