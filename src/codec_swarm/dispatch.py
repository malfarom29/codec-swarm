"""What each command does. The worker runs these; so does the dashboard when it runs without one (tests)."""

from __future__ import annotations

from typing import Any

from codec_swarm.service import MissionRequest, MissionService

KINDS = ("start", "answer", "recover", "restart", "restart_lane", "update", "pr_action")


async def execute(service: MissionService, ticket: str, kind: str, args: dict[str, Any]) -> None:
    """Run one command. A failure is written to the event log, where the dashboard shows it, then re-raised."""
    try:
        await _run(service, ticket, kind, args)
    except Exception as error:
        message = f"{type(error).__name__}: {error}"
        if kind == "pr_action":
            service.events.append(ticket, "pr.failed", {"lane": args.get("repo"), "action": args.get("action"), "error": message})
        else:
            service.events.append(ticket, "mission.error", {"error": message, "command": kind})
        raise


async def _run(service: MissionService, ticket: str, kind: str, a: dict[str, Any]) -> None:
    if kind == "start":
        await service.start(MissionRequest.model_validate(a["request"]))
    elif kind == "answer":
        if a.get("if_waiting") and not service.waiting_at_gate(ticket, a.get("lane") or ""):
            await service.recover(ticket)  # the answer already went through before the worker died
            return
        await service.answer(ticket, a.get("lane") or None, a["answer"], a.get("note", ""))
    elif kind == "recover":
        await service.recover(ticket)
    elif kind == "restart":
        await service.restart(ticket, MissionRequest.model_validate(a["request"]) if a.get("request") else None)
    elif kind == "restart_lane":
        await service.restart_lane(ticket, a["repo"], a.get("note", ""))
    elif kind == "update":
        message = await service.update_from_base(ticket, a["repo"], resolve_with_coder=bool(a.get("resolve")))
        service.events.append(ticket, "lane.update", {"lane": a["repo"], "message": message})
    elif kind == "pr_action":
        await service.pr_action(ticket, a["repo"], a["action"], a["target"])
    else:
        raise ValueError(f"unknown command {kind}")
