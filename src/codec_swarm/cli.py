"""codec-swarm command line: run a mission over one or more repos, and report on missions."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import anyio
from dotenv import load_dotenv

from codec_swarm.domain import Autonomy
from codec_swarm.graph import APPROVE, SEND_BACK
from codec_swarm.service import MissionRequest, MissionService
from codec_swarm.store import EventLog
from codec_swarm.store.metrics import compare, mission_metrics
from codec_swarm.workspace.lanes import DEFAULT_ROOT
from codec_swarm.workspace.repos import repo_name


def _print_events(events: EventLog, ticket: str, since: int) -> int:
    for e in events.list(ticket, since=since):
        p = e.payload
        if e.kind == "handoff":
            first = (p.get("summary") or "").strip().splitlines()[:1]
            print(f"  [{e.role}] handoff{' (sends back)' if p.get('send_back') else ''}: {first[0] if first else ''}")
        elif e.kind == "agent.tool":
            print(f"  [{e.role}] {p.get('tool')}")
        elif e.kind == "gate.decision" and p.get("action") != "allow":
            print(f"  [{e.role}] gate {p.get('action')}: {p.get('tool')} ({p.get('reason')})")
        elif e.kind == "cost":
            print(f"  [{e.role}] {p.get('turns')} turns · ${p.get('cost_usd') or 0:.3f}")
        elif e.kind == "verdict":
            print(f"  [judge] {p.get('band')} → {p.get('next')}\n" + "\n".join(f"    {line}" for line in (p.get("rationale") or "").splitlines()))
        elif e.kind == "decision" and p.get("source") == "jev":
            print(f"  [{e.role}] jev {p.get('slot')}: {p.get('next')} ({p.get('rationale')})")
        elif e.kind in ("decision", "agent.message", "gate.decision"):
            continue
        else:
            print(f"  {e.kind} {p if p else ''}")
        since = e.id
    return since


def _ask(lane: str | None, gate: dict, auto_approve: bool) -> str | None:
    """The gate's answer, or None to leave it waiting for a human."""
    where = f"lane {lane}" if lane else "mission"
    label = f"{where}: {gate.get('kind')} gate" + (f" after {gate['after']}" if gate.get("after") else "")
    failed = (gate.get("verdict") or {}).get("failed_checks")
    if auto_approve and failed:
        print(f"→ {label}: not approved, failed checks: {', '.join(failed)}. Waiting for a human.")
        return None
    if auto_approve:
        print(f"→ {label}: approved (--yes)")
        return APPROVE
    while True:
        answer = input(f"→ {label}. [a]pprove or [s]end back? ").strip().lower()
        if answer in ("a", "approve"):
            return APPROVE
        if answer in ("s", "send_back", "send back"):
            return SEND_BACK


def parse_bases(values: list[str], repos: list[str]) -> dict[str, str]:
    """--base develop (every repo) or --base api=release/1.2 (one repo)."""
    bases: dict[str, str] = {}
    for value in values:
        repo, _, branch = value.rpartition("=")
        for name in [repo] if repo else repos:
            if name not in repos:
                raise SystemExit(f"--base {value}: {name} is not one of this mission's repos ({', '.join(repos)})")
            bases[name] = branch
    return bases


async def run_mission(args: argparse.Namespace) -> int:
    service = MissionService(Path(args.root))
    request = MissionRequest(
        ticket=args.ticket, title=args.title, repo_urls=tuple(args.repo_url), description=args.description,
        autonomy=Autonomy(args.autonomy), pack=args.pack, model=args.model, no_jev=args.no_jev,
        bases=parse_bases(args.base, [repo_name(u) for u in args.repo_url]),
    )
    seen = max((e.id for e in service.events.list(args.ticket)), default=0)
    if args.recover:
        result = await service.recover(args.ticket)
    else:
        runtime = await service.build(request)
        print(f"pack {runtime.pack_name} · Jev {'on' if runtime.jev_on else 'off'}")
        for repo in runtime.mission.repos:
            print(f"lane {repo} · {service._workspace.worktrees / args.ticket / repo}")
        result = await service.start(request)
    seen = _print_events(service.events, args.ticket, seen)
    while not result.blocked and (waiting := result.waiting()):
        for lane, gate_info in waiting:
            answer = _ask(lane, gate_info, args.yes)
            if answer is not None:
                note = input("  Instructions for whoever picks it up (optional, Enter to skip): ").strip() if answer == SEND_BACK else ""
                result = await service.answer(args.ticket, lane, answer, note)
                seen = _print_events(service.events, args.ticket, seen)
                break  # the mission changed: re-read which gates are open
        else:
            break  # every open gate is waiting for a human
    await service.aclose()
    if result.blocked:
        print(f"\n{args.ticket}: blocked · {result.blocked}")
        return 1
    print()
    for repo, lane in result.lanes.items():
        print(f"{args.ticket} · {repo}: {lane.status}" + (f" · local PR: open {lane.pr_url} in `codec-swarm up`" if lane.pr_url else ""))
    return 0 if result.done else 1


def jev_on() -> bool:
    return bool(os.environ.get("TYPESAFE_API_KEY"))


def report(args: argparse.Namespace) -> int:
    metrics = []
    for root in args.root or [str(DEFAULT_ROOT)]:
        db = Path(root).expanduser() / "swarm.db"
        if not db.exists():
            continue
        events = EventLog(db)
        found = sorted({e.mission for e in events.list() if e.kind == "mission.started"})
        metrics += [mission_metrics(events.list(t)) for t in found if not args.tickets or t in args.tickets]
    metrics.sort(key=lambda m: m.ticket)
    print(f"{'ticket':<10} {'pack':<15} {'jev':<4} {'status':<9} {'min':>5} {'cost':>7} {'steps':>5} {'back':>4} {'judge':>5} {'wait':>5} {'asks':>4} {'jev tok':>8}")
    for m in metrics:
        to_pr = f"{m.minutes_to_pr:.1f}" if m.minutes_to_pr is not None else "-"
        print(f"{m.ticket:<10} {m.pack or '?':<15} {'on' if m.jev else 'off':<4} {m.status:<9} {to_pr:>5} ${m.agent_cost_usd:>6.2f} "
              f"{m.agent_steps:>5} {m.sendbacks:>4} {m.judge_runs:>5} {m.human_wait_minutes:>5.1f} {m.blocked_commands:>4} {m.jev_tokens:>8}")
    print()
    for r in compare(metrics):
        to_pr = f"{r.mean_minutes_to_pr:.1f} min" if r.mean_minutes_to_pr is not None else "-"
        print(f"{r.pack} · Jev {'on' if r.jev else 'off'}: {r.missions} missions, {r.pr_ready} PR ready · mean ${r.mean_cost_usd:.2f} · "
              f"{to_pr} to PR · {r.sendbacks_per_mission} send-backs · {r.judge_runs_per_mission} judge runs · "
              f"{r.human_wait_minutes_per_mission} min waiting on me · {r.blocked_commands_per_mission} asks")
    return 0


def up(args: argparse.Namespace) -> int:
    import secrets

    import uvicorn

    from codec_swarm.web.app import create_app

    # A new token every launch; the URL below carries it. CODEC_SWARM_TOKEN pins it for local previews.
    token = os.environ.get("CODEC_SWARM_TOKEN") or secrets.token_urlsafe(18)
    service = MissionService(Path(args.root))
    app = create_app(service, token)
    print(f"codec-swarm dashboard: http://127.0.0.1:{args.port}/?token={token}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")  # localhost only
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    # `uv run codec-swarm` exports codec-swarm's own venv; agents and checks must use the target repo's.
    os.environ.pop("VIRTUAL_ENV", None)
    parser = argparse.ArgumentParser(prog="codec-swarm")
    sub = parser.add_subparsers(dest="command", required=True)
    m = sub.add_parser("mission", help="run one mission on one repo, from the first role to its PR")
    m.add_argument("--repo-url", required=True, action="append", help="repeat for a mission that spans several repos")
    m.add_argument("--ticket", required=True)
    m.add_argument("--title", required=True)
    m.add_argument("--description", default="")
    m.add_argument("--base", action="append", default=[], metavar="[REPO=]BRANCH", help="branch to start from instead of the configured base; REPO= picks one repo")
    m.add_argument("--autonomy", choices=[a.value for a in Autonomy], default=Autonomy.GATED.value)
    m.add_argument("--pack", default="auto", help="auto (rule, or Jev when on), solo, codec-standard or a pack path")
    m.add_argument("--model", help="force one model for every role (a local override; beats Jev's pick)")
    m.add_argument("--no-jev", action="store_true", help="use the fixed rules, allowlist and checks-only judge")
    m.add_argument("--root", default=str(DEFAULT_ROOT))
    m.add_argument("--recover", action="store_true", help="continue a mission that crashed mid-step")
    m.add_argument("--yes", action="store_true", help="approve every gate without asking (sandbox repos only)")
    u = sub.add_parser("up", help="serve the dashboard on localhost")
    u.add_argument("--port", type=int, default=8765)
    u.add_argument("--root", default=str(DEFAULT_ROOT))
    r = sub.add_parser("report", help="metrics per mission and the pack / Jev comparison")
    r.add_argument("tickets", nargs="*")
    r.add_argument("--root", action="append", help="workspace root to read; repeat to combine several (default ~/.codec-swarm)")
    args = parser.parse_args(argv)
    if args.command == "mission":
        return anyio.run(run_mission, args)
    if args.command == "report":
        return report(args)
    if args.command == "up":
        return up(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
