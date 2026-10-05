"""codec-swarm command line. M2: run one mission against one repo from the terminal."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import anyio
from dotenv import load_dotenv

from codec_swarm.domain import Autonomy, Mission
from codec_swarm.graph import APPROVE, SEND_BACK, MissionRunner, RunResult
from codec_swarm.harness import LocalOverrides, load_pack, load_repo_config, resolve_session
from codec_swarm.plugins.claude_code import ClaudeCodeBackend
from codec_swarm.plugins.jev import JevClient, LaneUnderJudgement
from codec_swarm.plugins.registry import build_plugins
from codec_swarm.store import EventLog, GateCache, SessionStore
from codec_swarm.workspace import Workspace, WorkspaceRecorder
from codec_swarm.workspace.github import GitHubPublisher
from codec_swarm.workspace.lanes import DEFAULT_ROOT


def _repo_name(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")


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


def _ask(result: RunResult, auto_approve: bool) -> str | None:
    """The gate's answer, or None to leave the mission waiting at the gate."""
    gate = result.gate or {}
    label = f"{gate.get('kind')} gate" + (f" after {gate['after']}" if gate.get("after") else "")
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


async def run_mission(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    pack = load_pack(args.pack)
    workspace = Workspace(root)
    repo = _repo_name(args.repo_url)
    clone = workspace.clone(args.repo_url, repo)
    config = load_repo_config(clone, pack)
    lane = workspace.prepare_lane(args.repo_url, repo, args.ticket, args.title, config.branch_flow)
    print(f"lane {lane.ticket} · {lane.repo} · {lane.branch}\n  {lane.path}")

    mission = Mission(ticket=args.ticket, repo=repo, title=args.title, description=args.description, autonomy=Autonomy(args.autonomy), bands=config.judge)
    db = root / "swarm.db"
    events, sessions = EventLog(db), SessionStore(db)
    overrides = LocalOverrides(model=args.model)
    jev = JevClient()
    judged = LaneUnderJudgement(worktree=lane.path, base=lane.base, checks=config.checks)
    plugins = build_plugins(pack, config, lambda m: judged, GateCache(db), use_jev=not args.no_jev, ask=None if args.no_jev else jev)
    print(f"  Jev {'on: routing, command gate and judge' if plugins.jev else 'off: fixed rules, allowlist and checks-only judge'}")
    backend = ClaudeCodeBackend(
        lambda m, role: resolve_session(pack, config, role, lane.path, m, overrides=overrides),
        plugins.gate,
        sessions,
    )
    lanes = {lane.ticket: lane}
    runner = MissionRunner(
        db,
        pack.pack,
        backend,
        plugins.router,
        plugins.judge,
        events,
        recorder=WorkspaceRecorder(workspace, lanes),
        publisher=GitHubPublisher(lanes),
    )

    seen = max((e.id for e in events.list(args.ticket)), default=0)
    result = await (runner.recover(args.ticket) if args.recover else runner.start(mission))
    seen = _print_events(events, args.ticket, seen)
    while result.status == "waiting":
        answer = _ask(result, args.yes)
        if answer is None:
            break
        result = await runner.answer(args.ticket, answer)
        seen = _print_events(events, args.ticket, seen)
    await jev.aclose()
    if jev.calls:
        tokens = sum((c.get("input_tokens") or 0) + (c.get("output_tokens") or 0) for c in jev.calls)
        print(f"  Jev: {len(jev.calls)} calls · {tokens} tokens")
    print(f"\n{args.ticket}: {result.status}" + (f" · {result.pr_url}" if result.pr_url else ""))
    return 0 if result.status == "pr_ready" else 1


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    # `uv run codec-swarm` exports codec-swarm's own venv; agents and checks must use the target repo's.
    os.environ.pop("VIRTUAL_ENV", None)
    parser = argparse.ArgumentParser(prog="codec-swarm")
    sub = parser.add_subparsers(dest="command", required=True)
    m = sub.add_parser("mission", help="run one mission on one repo, from the first role to its PR")
    m.add_argument("--repo-url", required=True)
    m.add_argument("--ticket", required=True)
    m.add_argument("--title", required=True)
    m.add_argument("--description", default="")
    m.add_argument("--autonomy", choices=[a.value for a in Autonomy], default=Autonomy.GATED.value)
    m.add_argument("--pack", default="codec-standard")
    m.add_argument("--model", help="force one model for every role (a local override; beats Jev's pick)")
    m.add_argument("--no-jev", action="store_true", help="use the fixed rules, allowlist and checks-only judge")
    m.add_argument("--root", default=str(DEFAULT_ROOT))
    m.add_argument("--recover", action="store_true", help="continue a mission that crashed mid-step")
    m.add_argument("--yes", action="store_true", help="approve every gate without asking (sandbox repos only)")
    args = parser.parse_args(argv)
    if args.command == "mission":
        return anyio.run(run_mission, args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
