"""The dashboard: FastAPI + Jinja + htmx. Reads come from the event log; actions go through the mission service."""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import anyio
from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from codec_swarm.domain import Autonomy, JudgeBands
from codec_swarm.harness import load_pack
from codec_swarm.plugins.jev import MODEL as JEV_MODEL
from codec_swarm.plugins.registry import jev_available
from codec_swarm.service import MissionRequest, MissionService
from codec_swarm.store.events import Event
from codec_swarm.store.views import MissionView, mission_view

HERE = Path(__file__).parent
COOKIE = "codec_session"
STAGES = [("planning", "Planning"), ("building", "Building"), ("review", "Waiting on you"), ("pr_ready", "PR ready"), ("blocked", "Blocked")]
GATE_BAND = (0.90, 0.03)  # command gate threshold and margin, as measured in spikes/jev_command_gate.py


def _line(e: Event) -> str | None:
    """One activity-feed line per event worth reading."""
    p = e.payload
    lane = f"[{p['lane']}] " if p.get("lane") else ""
    who = e.role or ""
    if e.kind == "handoff":
        first = (p.get("summary") or "").strip().splitlines()[:1]
        return f"{lane}{who} handed off{' (sends back)' if p.get('send_back') else ''}: {first[0] if first else ''}"
    if e.kind == "verdict":
        score = f"{p['score']:.2f}" if p.get("score") is not None else "no score"
        return f"{lane}judge: {score}, {p.get('band')} band → {p.get('next')}"
    if e.kind == "gate.opened":
        return f"{lane}{p.get('kind')} gate opened"
    if e.kind == "gate.resolved":
        return f"{lane}{p.get('kind')} gate: {p.get('answer')}"
    if e.kind == "decision" and p.get("source") == "jev":
        return f"{lane}{who}: Jev picked {p.get('next')} for {p.get('slot')}"
    if e.kind in ("lane.started", "lane.pushed", "lane.failed", "pr.opened", "mission.blocked", "mission.error", "mission.planned"):
        detail = p.get("branch") or p.get("url") or p.get("error") or p.get("reason") or ""
        return f"{lane or ('[' + p['lane'] + '] ' if p.get('lane') else '')}{e.kind.replace('.', ' ')} {detail}".strip()
    return None


def _terminal(events: list[Event], repo: str, limit: int = 80) -> list[str]:
    lines = []
    for e in events:
        p = e.payload
        if p.get("lane") != repo:
            continue
        if e.kind == "agent.message":
            lines.append(f"{e.role}› {(p.get('text') or '').strip()}")
        elif e.kind == "agent.tool":
            detail = p.get("input", {}).get("command") or p.get("input", {}).get("file_path") or ""
            lines.append(f"{e.role}  ● {p.get('tool')} {detail}".rstrip())
        elif e.kind == "gate.decision" and p.get("action") != "allow":
            lines.append(f"{e.role}  ▲ gate {p.get('action')}: {p.get('reason')}")
        elif e.kind == "cost":
            lines.append(f"{e.role}  ✓ {p.get('turns')} turns · ${p.get('cost_usd') or 0:.3f}")
    return lines[-limit:]


def create_app(service: MissionService, token: str) -> FastAPI:
    app = FastAPI(title="codec-swarm", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")

    @app.middleware("http")
    async def require_token(request: Request, call_next: Callable[[Request], Awaitable[Any]]):
        if request.url.path.startswith("/static/"):
            return await call_next(request)
        given = request.query_params.get("token")
        if given is not None and secrets.compare_digest(given, token):
            rest = {k: v for k, v in request.query_params.items() if k != "token"}
            response = RedirectResponse(request.url.path + (f"?{urlencode(rest)}" if rest else ""), status_code=303)
            response.set_cookie(COOKIE, token, httponly=True, samesite="strict")
            return response
        if secrets.compare_digest(request.cookies.get(COOKIE, ""), token):
            return await call_next(request)
        return PlainTextResponse("Open the URL `codec-swarm up` printed: it carries this launch's token.", status_code=401)

    def missions() -> list[MissionView]:
        tickets = sorted({e.mission for e in service.events.list() if e.kind == "mission.started"}, reverse=True)
        return [mission_view(service.events.list(t)) for t in tickets]

    def last_id() -> int:
        rows = service.events.list()
        return rows[-1].id if rows else 0

    def render(request: Request, name: str, **context: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {"last_id": last_id(), "jev_on": jev_available(), **context})

    async def in_background(ticket: str, work: Callable[[], Awaitable[Any]]) -> None:
        try:
            await work()
        except Exception as error:  # surface failures on the page instead of losing them in a server log
            service.events.append(ticket, "mission.error", {"error": f"{type(error).__name__}: {error}"})

    # --- pages ----------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def board(request: Request):
        return render(request, "board.html", stages=STAGES, missions=missions())

    @app.get("/partials/board", response_class=HTMLResponse)
    async def board_partial(request: Request):
        return render(request, "_board.html", stages=STAGES, missions=missions())

    @app.get("/inbox", response_class=HTMLResponse)
    async def inbox(request: Request):
        return render(request, "inbox.html", gates=[g for m in missions() for g in m.open_gates], titles={m.ticket: m.title for m in missions()})

    @app.get("/partials/inbox", response_class=HTMLResponse)
    async def inbox_partial(request: Request):
        return render(request, "_inbox.html", gates=[g for m in missions() for g in m.open_gates], titles={m.ticket: m.title for m in missions()})

    def mission_context(ticket: str) -> dict[str, Any]:
        events = service.events.list(ticket)
        view = mission_view(events)
        activity = [(e.created_at[11:19], line) for e in events if (line := _line(e))][-60:][::-1]
        terminals = {repo: _terminal(events, repo) for repo in view.lanes}
        return {"m": view, "activity": activity, "terminals": terminals}

    @app.get("/missions/new", response_class=HTMLResponse)
    async def new_mission(request: Request):
        return render(request, "new_mission.html")

    @app.get("/missions/{ticket}", response_class=HTMLResponse)
    async def mission(request: Request, ticket: str):
        if not service.events.list(ticket):
            return PlainTextResponse(f"No mission {ticket}", status_code=404)
        return render(request, "mission.html", **mission_context(ticket))

    @app.get("/partials/missions/{ticket}", response_class=HTMLResponse)
    async def mission_partial(request: Request, ticket: str):
        return render(request, "_mission.html", **mission_context(ticket))

    @app.get("/harness", response_class=HTMLResponse)
    async def harness(request: Request):
        packs = [load_pack(name) for name in ("codec-standard", "solo")]
        return render(request, "harness.html", packs=packs)

    @app.get("/orchestration", response_class=HTMLResponse)
    async def orchestration(request: Request):
        threshold, margin = GATE_BAND
        return render(
            request, "orchestration.html", bands=JudgeBands(), jev_model=JEV_MODEL,
            gate_run=threshold + margin, gate_unsure=threshold - margin,
        )

    # --- actions --------------------------------------------------------------

    @app.post("/missions")
    async def start_mission(
        background: BackgroundTasks,
        ticket: str = Form(...),
        title: str = Form(...),
        repo_urls: str = Form(...),
        description: str = Form(""),
        autonomy: str = Form("gated"),
        pack: str = Form("auto"),
        model: str = Form(""),
        jev: str = Form(""),
    ):
        request = MissionRequest(
            ticket=ticket.strip(), title=title.strip(), description=description.strip(),
            repo_urls=tuple(u.strip() for u in repo_urls.splitlines() if u.strip()),
            autonomy=Autonomy(autonomy), pack=pack, model=model.strip() or None, no_jev=not jev,
        )
        background.add_task(in_background, request.ticket, lambda: service.start(request))
        return RedirectResponse(f"/missions/{request.ticket}", status_code=303)

    @app.post("/missions/{ticket}/gates")
    async def answer_gate(background: BackgroundTasks, ticket: str, lane: str = Form(""), answer: str = Form(...), back: str = Form("")):
        background.add_task(in_background, ticket, lambda: service.answer(ticket, lane or None, answer))
        return RedirectResponse(back or f"/missions/{ticket}", status_code=303)

    # --- live feed --------------------------------------------------------------

    @app.get("/events/stream")
    async def stream(since: int = 0, once: bool = False):
        async def changes():
            last = since
            while True:
                rows = service.events.list(since=last)
                if rows:
                    last = rows[-1].id
                    yield {"event": "change", "data": str(last)}
                    if once:
                        return
                elif once:
                    return
                await anyio.sleep(1)

        return EventSourceResponse(changes())

    return app
