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


class UpstreamLane(BaseModel, frozen=True):
    """A lane this lane depends on, and the branch the orchestrator pushed once it reached the judge."""

    repo: str
    branch: str


class Mission(BaseModel, frozen=True):
    ticket: str
    repo: str  # this lane's repo; "" while planning a mission that spans several repos
    repos: tuple[str, ...] = ()  # every repo in the mission, in the order given
    upstream: tuple[UpstreamLane, ...] = ()  # lanes this lane codes against, with their pushed branches
    title: str = ""
    description: str = ""
    autonomy: Autonomy = Autonomy.GATED
    bands: JudgeBands | None = None


class LaneOrder(BaseModel, frozen=True):
    """One lane in the Architect's plan: which repos must reach the judge before it starts."""

    repo: str
    after: tuple[str, ...] = ()


class Handoff(BaseModel, frozen=True, str_strip_whitespace=True):
    from_role: str
    summary: str
    send_back: bool = False
    commit_message: str = ""  # Conventional Commits message for the step's code changes; the orchestrator commits
    incomplete: bool = False  # the step ended without a structured handoff, even after one retry
    lane_order: tuple[LaneOrder, ...] = ()  # only from the role that plans lanes
    files_touched: tuple[str, ...] = ()
    commit_sha: str | None = None
    questions: tuple[str, ...] = ()


class Decision(BaseModel, frozen=True):
    """A routing decision with where it came from, so the UI can show Jev chips or fixed-rule chips."""

    next: str
    source: str
    rationale: str = ""
    options: dict[str, float] = {}  # what was considered, with probabilities when the source has them


class ScenarioResult(BaseModel, frozen=True):
    name: str
    probability: float | None = None  # Jev's "is this scenario met?"; None for judges without a calibrated score


class Verdict(BaseModel, frozen=True):
    score: float | None  # None when the judge gives no calibrated score
    source: str
    rationale: str = ""
    failed_checks: tuple[str, ...] = ()
    scenarios: tuple[ScenarioResult, ...] = ()


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
            # Without a calibrated score nothing approves itself: a failed check goes back, a clean run goes to a human.
            return Band.STOP if verdict.failed_checks else Band.REVIEW
        if verdict.score >= self.approve_at:
            # A failed hard check blocks auto-approval regardless of the score.
            return Band.REVIEW if verdict.failed_checks else Band.APPROVE
        return Band.REVIEW if verdict.score >= self.review_at else Band.STOP

    def tightened(self, other: JudgeBands) -> JudgeBands:
        """A mission may tighten the repo's thresholds, never loosen them."""
        return JudgeBands(approve_at=max(self.approve_at, other.approve_at), review_at=max(self.review_at, other.review_at))


Mission.model_rebuild()
