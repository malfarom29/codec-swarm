"""Pure domain model: no I/O, no framework imports beyond pydantic."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, model_validator


class Autonomy(StrEnum):
    MANUAL = "manual"  # every handoff waits for me
    GATED = "gated"  # spec gate, review-band verdicts and the PR gate wait for me
    AUTO = "auto"  # only review-band verdicts wait for me


class GateKind(StrEnum):
    SPEC = "spec"
    HANDOFF = "handoff"
    REVIEW = "review"
    PR = "pr"


class Band(StrEnum):
    APPROVE = "approve"
    REVIEW = "review"
    STOP = "stop"


class Pack(BaseModel, frozen=True):
    """A role pack: planning roles run once per mission, lane roles run per repo."""

    name: str
    planning_roles: tuple[str, ...]
    lane_roles: tuple[str, ...]
    max_rework: int = 2  # judge send-backs before the lane goes to human review

    @property
    def rework_role(self) -> str:
        return self.lane_roles[0]

    def next_role(self, role: str) -> str | None:
        for roles in (self.planning_roles, self.lane_roles):
            if role in roles:
                i = roles.index(role)
                return roles[i + 1] if i + 1 < len(roles) else None
        raise ValueError(f"{role} is not in pack {self.name}")

    def previous_lane_role(self, role: str) -> str | None:
        i = self.lane_roles.index(role)
        return self.lane_roles[i - 1] if i > 0 else None


CODEC_STANDARD = Pack(
    name="codec-standard",
    planning_roles=("specifier", "architect"),
    lane_roles=("backend-coder", "reviewer", "hardener", "qa"),
)


class Mission(BaseModel, frozen=True):
    ticket: str
    repo: str
    title: str = ""
    autonomy: Autonomy = Autonomy.GATED
    bands: JudgeBands | None = None


class Handoff(BaseModel, frozen=True):
    from_role: str
    summary: str
    send_back: bool = False
    files_touched: tuple[str, ...] = ()
    commit_sha: str | None = None
    questions: tuple[str, ...] = ()


class Decision(BaseModel, frozen=True):
    """A routing decision with where it came from, so the UI can show Jev chips or fixed-rule chips."""

    next: str
    source: str
    rationale: str = ""


class Verdict(BaseModel, frozen=True):
    score: float | None  # None when the judge gives no calibrated score
    source: str
    rationale: str = ""
    failed_checks: tuple[str, ...] = ()


class JudgeBands(BaseModel, frozen=True):
    approve_at: float = 0.95
    review_at: float = 0.80

    @model_validator(mode="after")
    def _ordered(self) -> JudgeBands:
        if not 0.0 <= self.review_at <= self.approve_at <= 1.0:
            raise ValueError("need 0 <= review_at <= approve_at <= 1")
        return self

    def classify(self, verdict: Verdict) -> Band:
        if verdict.score is None:
            return Band.REVIEW
        if verdict.score >= self.approve_at:
            # A failed hard check blocks auto-approval regardless of the score.
            return Band.REVIEW if verdict.failed_checks else Band.APPROVE
        return Band.REVIEW if verdict.score >= self.review_at else Band.STOP

    def tightened(self, other: JudgeBands) -> JudgeBands:
        """A mission may tighten the repo's thresholds, never loosen them."""
        return JudgeBands(approve_at=max(self.approve_at, other.approve_at), review_at=max(self.review_at, other.review_at))


Mission.model_rebuild()
