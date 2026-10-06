"""What the dashboard shows, derived from the event log alone: cheap to compute and identical after a restart."""

from __future__ import annotations

from pydantic import BaseModel

from codec_swarm.store.events import Event

PLANNING = ""  # gate events from the planning thread carry an empty lane

# Board columns by stage, in order: a mission sits in the column of its least advanced lane.
STAGES = ("spec", "build", "review", "judge", "pr_ready")
ROLE_STAGE = {"specifier": "spec", "architect": "spec", "backend-coder": "build", "frontend-coder": "build", "coder": "build",
              "reviewer": "review", "hardener": "review", "qa": "review"}


class RoleStats(BaseModel):
    role: str
    model: str | None = None
    steps: int = 0
    turns: int = 0
    cost_usd: float = 0.0
    sent_back: int = 0


class GateView(BaseModel):
    ticket: str
    lane: str  # "" for the planning gate
    kind: str
    after: str | None = None
    verdict: dict | None = None
    opened_at: str


class LaneView(BaseModel):
    repo: str
    status: str = "not_started"  # not_started | running | waiting | pr_ready | failed
    gate: GateView | None = None
    pr_url: str | None = None  # the local PR, reviewed in the dashboard
    pushed_url: str | None = None  # the GitHub PR, once I pushed it
    merged_into: str | None = None  # the local branch I merged it into
    verdicts: list[dict] = []
    current_role: str | None = None
    after: list[str] = []
    roles: dict[str, RoleStats] = {}

    @property
    def stage(self) -> str:
        if self.status == "failed":
            return "blocked"
        if self.status == "pr_ready":
            return "pr_ready"
        if self.gate and self.gate.kind in ("review", "pr"):
            return "judge"
        if self.status == "not_started":
            return "spec"
        return ROLE_STAGE.get(self.current_role or "", "build")


class MissionView(BaseModel):
    ticket: str
    title: str = ""
    description: str = ""
    repos: list[str] = []
    pack: str | None = None
    jev: bool | None = None
    autonomy: str | None = None
    stage: str = "spec"  # spec | build | review | judge | pr_ready | blocked
    planning_gate: GateView | None = None
    lanes: dict[str, LaneView] = {}
    planning_roles: dict[str, RoleStats] = {}
    planning_role: str | None = None  # the planning role working right now, if any
    blocked: str | None = None
    cost_usd: float = 0.0
    earlier_cost_usd: float = 0.0  # what runs before the last restart cost
    runs: int = 1
    started_at: str = ""
    last_event_id: int = 0

    @property
    def open_gates(self) -> list[GateView]:
        gates = [self.planning_gate] if self.planning_gate else []
        return gates + [lane.gate for lane in self.lanes.values() if lane.gate]


def mission_view(events: list[Event]) -> MissionView:
    if not events:
        raise ValueError("no events for this mission")
    view = MissionView(ticket=events[0].mission, started_at=events[0].created_at)
    open_gates: dict[str, GateView] = {}
    for e in events:
        p = e.payload
        if e.kind == "mission.restarted":  # what came before belongs to an earlier run
            view = MissionView(ticket=view.ticket, started_at=e.created_at, earlier_cost_usd=view.earlier_cost_usd + view.cost_usd, runs=view.runs + 1)
            open_gates = {}
        view.last_event_id = e.id
        if e.kind == "mission.started":
            request = p.get("request") or {}
            view.title, view.description = request.get("title", ""), request.get("description", "")
            view.repos = list(p.get("repos") or ([p["repo"]] if p.get("repo") else []))
            view.pack, view.jev, view.autonomy = p.get("pack"), p.get("jev"), p.get("autonomy")
            view.lanes = {r: LaneView(repo=r) for r in view.repos}
        elif e.kind == "cost":
            view.cost_usd += p.get("cost_usd") or 0.0
            if e.role and (stats := _role_stats(view, p, e.role)) is not None:
                stats.turns += p.get("turns") or 0
                stats.cost_usd += p.get("cost_usd") or 0.0
        elif e.kind == "decision" and p.get("slot") == "model" and e.role:
            if (stats := _role_stats(view, p, e.role)) is not None:
                stats.model = p.get("next")
        elif e.kind == "lane.started":
            lane = view.lanes.setdefault(p["lane"], LaneView(repo=p["lane"]))
            lane.status, lane.after = "running", list(p.get("after") or [])
        elif e.kind == "lane.failed":
            view.lanes.setdefault(p["lane"], LaneView(repo=p["lane"])).status = "failed"
        elif e.kind == "mission.blocked":
            view.blocked = p.get("reason")
        elif e.kind == "gate.opened":
            if (lane := _lane_of(p, view, p.get("kind"))) is not None:
                open_gates[lane] = GateView(ticket=view.ticket, lane=lane, kind=p["kind"], after=p.get("after"), verdict=p.get("verdict"), opened_at=e.created_at)
        elif e.kind == "gate.resolved":
            lane = p.get("lane")
            if lane is None:  # older logs: close the oldest open gate of this kind
                lane = next((k for k, g in open_gates.items() if g.kind == p.get("kind")), None)
            open_gates.pop(lane, None)
        elif e.kind == "verdict" and (lane := _lane_of(p, view)):
            view.lanes.setdefault(lane, LaneView(repo=lane)).verdicts.append(p)
        elif e.kind in ("agent.message", "agent.tool", "handoff") and e.role and (lane := _lane_of(p, view)):
            view.lanes.setdefault(lane, LaneView(repo=lane)).current_role = e.role
        elif e.kind in ("agent.message", "agent.tool") and e.role and p.get("lane") == PLANNING:
            view.planning_role = e.role
        if e.kind == "handoff" and e.role and (stats := _role_stats(view, p, e.role)) is not None:
            stats.steps += 1
            stats.sent_back += bool(p.get("send_back"))
            if p.get("lane") == PLANNING:
                view.planning_role = None
        if e.kind == "pr.local" and (lane := _lane_of(p, view)):
            view.lanes.setdefault(lane, LaneView(repo=lane)).pr_url = p.get("url")
        elif e.kind == "pr.pushed" and p.get("lane"):
            view.lanes.setdefault(p["lane"], LaneView(repo=p["lane"])).pushed_url = p.get("url")
        elif e.kind == "pr.merged" and p.get("lane"):
            view.lanes.setdefault(p["lane"], LaneView(repo=p["lane"])).merged_into = p.get("target")
        if e.kind == "mission.planned":
            view.planning_role = None
        elif e.kind == "mission.done":
            lane = _lane_of(p, view) or next((r for r, ln in view.lanes.items() if ln.status != "pr_ready"), None)
            if lane:
                target = view.lanes.setdefault(lane, LaneView(repo=lane))
                target.status, target.pr_url, target.current_role = "pr_ready", p.get("pr_url") or target.pr_url, None
                open_gates.pop(lane, None)
    view.planning_gate = open_gates.pop(PLANNING, None)
    for repo, gate in open_gates.items():
        lane = view.lanes.setdefault(repo, LaneView(repo=repo))
        lane.status, lane.gate = "waiting", gate
    for lane in view.lanes.values():
        if lane.status in ("pr_ready", "waiting", "failed"):
            lane.current_role = None
    view.stage = _stage(view)
    return view


def _lane_of(payload: dict, view: MissionView, gate_kind: str | None = None) -> str | None:
    """The event's lane. Older logs carry none: a spec gate is planning's, anything else is the only repo's."""
    if payload.get("lane") is not None:
        return payload["lane"]
    if gate_kind == "spec":
        return PLANNING
    return view.repos[0] if len(view.repos) == 1 else None


def _role_stats(view: MissionView, payload: dict, role: str) -> RoleStats | None:
    if payload.get("lane") == PLANNING and role in ROLE_STAGE and ROLE_STAGE[role] == "spec":
        return view.planning_roles.setdefault(role, RoleStats(role=role))
    lane = _lane_of(payload, view)
    if not lane:
        return None
    return view.lanes.setdefault(lane, LaneView(repo=lane)).roles.setdefault(role, RoleStats(role=role))


def _stage(view: MissionView) -> str:
    """The least advanced lane's stage; blocked wins, and nothing started yet is still spec."""
    if view.blocked or any(lane.status == "failed" for lane in view.lanes.values()):
        return "blocked"
    if not view.lanes:
        return "spec"
    return min((lane.stage for lane in view.lanes.values()), key=STAGES.index)
