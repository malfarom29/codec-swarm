"""Mission metrics derived from the event log, and the Solo-vs-swarm, Jev-vs-rules comparison built on them."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from statistics import mean

from pydantic import BaseModel

from codec_swarm.store.events import Event

NOT_AGENTS = {"judge", "human"}


class MissionMetrics(BaseModel, frozen=True):
    ticket: str
    pack: str | None = None
    jev: bool | None = None
    status: str = "running"
    pr_url: str | None = None
    minutes_to_pr: float | None = None
    agent_steps: int = 0
    sendbacks: int = 0
    judge_runs: int = 0
    gates: int = 0
    human_wait_minutes: float = 0.0  # gate opened to gate resolved: my review time
    agent_cost_usd: float = 0.0
    agent_turns: int = 0
    jev_calls: int = 0
    jev_tokens: int = 0
    blocked_commands: int = 0


def _at(event: Event) -> datetime:
    return datetime.fromisoformat(event.created_at.replace("Z", "+00:00"))


def mission_metrics(events: list[Event]) -> MissionMetrics:
    if not events:
        raise ValueError("no events for this mission")
    m: dict = {"ticket": events[0].mission}
    started = _at(events[0])
    opened: datetime | None = None
    wait = 0.0
    for e in events:
        p = e.payload
        if e.kind == "mission.started":
            started, m["pack"], m["jev"] = _at(e), p.get("pack"), p.get("jev")
        elif e.kind == "handoff" and e.role not in NOT_AGENTS:
            m["agent_steps"] = m.get("agent_steps", 0) + 1
            m["sendbacks"] = m.get("sendbacks", 0) + bool(p.get("send_back"))
        elif e.kind == "verdict":
            m["judge_runs"] = m.get("judge_runs", 0) + 1
        elif e.kind == "gate.opened":
            m["gates"], opened = m.get("gates", 0) + 1, _at(e)
        elif e.kind == "gate.resolved" and opened is not None:
            wait, opened = wait + (_at(e) - opened).total_seconds() / 60, None
        elif e.kind == "cost":
            m["agent_cost_usd"] = m.get("agent_cost_usd", 0.0) + (p.get("cost_usd") or 0.0)
            m["agent_turns"] = m.get("agent_turns", 0) + (p.get("turns") or 0)
        elif e.kind == "jev.usage":
            m["jev_calls"] = m.get("jev_calls", 0) + p.get("calls", 0)
            m["jev_tokens"] = m.get("jev_tokens", 0) + p.get("tokens", 0)
        elif e.kind == "gate.decision" and p.get("action") == "ask":
            m["blocked_commands"] = m.get("blocked_commands", 0) + 1
        elif e.kind == "mission.done":
            m["status"], m["pr_url"] = p.get("status", "pr_ready"), p.get("pr_url")
            m["minutes_to_pr"] = round((_at(e) - started).total_seconds() / 60, 1)
    if opened is not None:
        m["status"] = "waiting"
    # Missions recorded before labels existed: infer them from what the log shows.
    if m.get("jev") is None:
        m["jev"] = any(e.kind == "decision" and e.payload.get("source") == "jev" for e in events)
    if m.get("pack") is None:
        m["pack"] = "solo" if any(e.kind == "handoff" and e.role == "coder" for e in events) else "codec-standard"
    return MissionMetrics(**m, human_wait_minutes=round(wait, 1))


class ComparisonRow(BaseModel, frozen=True):
    pack: str
    jev: bool
    missions: int
    pr_ready: int
    mean_cost_usd: float
    mean_minutes_to_pr: float | None
    sendbacks_per_mission: float
    judge_runs_per_mission: float
    human_wait_minutes_per_mission: float
    blocked_commands_per_mission: float
    jev_tokens_per_mission: float


def compare(missions: list[MissionMetrics]) -> list[ComparisonRow]:
    groups: dict[tuple[str, bool], list[MissionMetrics]] = defaultdict(list)
    for m in missions:
        groups[(m.pack or "unknown", bool(m.jev))].append(m)
    rows = []
    for (pack, jev), ms in sorted(groups.items()):
        to_pr = [m.minutes_to_pr for m in ms if m.minutes_to_pr is not None]
        rows.append(
            ComparisonRow(
                pack=pack,
                jev=jev,
                missions=len(ms),
                pr_ready=sum(m.status == "pr_ready" for m in ms),
                mean_cost_usd=round(mean(m.agent_cost_usd for m in ms), 3),
                mean_minutes_to_pr=round(mean(to_pr), 1) if to_pr else None,
                sendbacks_per_mission=round(mean(m.sendbacks for m in ms), 2),
                judge_runs_per_mission=round(mean(m.judge_runs for m in ms), 2),
                human_wait_minutes_per_mission=round(mean(m.human_wait_minutes for m in ms), 1),
                blocked_commands_per_mission=round(mean(m.blocked_commands for m in ms), 2),
                jev_tokens_per_mission=round(mean(m.jev_tokens for m in ms)),
            )
        )
    return rows
