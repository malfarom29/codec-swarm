"""The dashboard: FastAPI + Jinja + htmx. Reads come from the event log; actions go through the mission service."""

from __future__ import annotations

import secrets
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime
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
from codec_swarm.harness.packs import MODEL_ORDER
from codec_swarm.plugins.jev import MODEL as JEV_MODEL
from codec_swarm.plugins.jira import JiraError
from codec_swarm.plugins.registry import jev_available
from codec_swarm.service import MissionRequest, MissionService
from codec_swarm.store.settings import Orchestration, RoleOverride
from codec_swarm.store.events import Event
from codec_swarm.store.views import MissionView, mission_view

HERE = Path(__file__).parent
COOKIE = "codec_session"
STAGES = [("intake", "Intake", "from Jira"), ("spec", "Spec", "Gherkin"), ("build", "Build", "TDD"), ("review", "Review", "code and QA"), ("judge", "Judge", "Definition of Done"), ("pr_ready", "PR ready", "branch-flow"), ("blocked", "Blocked", "failed or stuck")]
AGENTS = [("specifier", "Specifier"), ("architect", "Architect"), ("backend-coder", "Backend coder"), ("frontend-coder", "Frontend coder"), ("coder", "Coder (Solo)"), ("reviewer", "Reviewer"), ("hardener", "Hardener"), ("qa", "QA")]
JIRA_SYNC_SECONDS = 300
PACKS = ("codec-standard", "solo")
# What I see on "My requests": plain steps instead of the engine's stages.
REQUEST_STEPS = ["Writing the spec", "Your OK on the spec", "Building", "Checking the work", "Ready for review"]


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


def _terminals(events: list[Event], repo: str, limit: int = 80) -> dict[str, list[str]]:
    """Each role's output in one lane, like the terminal it ran in."""
    by_role: dict[str, list[str]] = {}
    for e in events:
        if e.payload.get("lane") == repo and e.role:
            line = _terminal_line(e)
            if line:
                by_role.setdefault(e.role, []).append(line)
    return {role: lines[-limit:] for role, lines in by_role.items()}


def _terminal_line(e: Event) -> str | None:
    p = e.payload
    if e.kind == "agent.message":
        return f"› {(p.get('text') or '').strip()}"
    if e.kind == "agent.tool":
        detail = p.get("input", {}).get("command") or p.get("input", {}).get("file_path") or ""
        return f"● {p.get('tool')} {detail}".rstrip()
    if e.kind == "gate.decision" and p.get("action") != "allow":
        return f"▲ gate {p.get('action')}: {p.get('reason')}"
    if e.kind == "cost":
        return f"✓ {p.get('turns')} turns · ${p.get('cost_usd') or 0:.3f}"
    return None


def request_step(m: MissionView) -> int:
    if m.stage == "spec":
        return 1 if m.planning_gate else 0
    return {"build": 2, "review": 3, "judge": 3, "pr_ready": 4}.get(m.stage, 2)


def open_questions(events: list[Event]) -> list[tuple[str, str]]:
    """The questions in each role's latest handoff, as (role, question)."""
    latest: dict[tuple[str, str], list[str]] = {}
    for e in events:
        if e.kind == "handoff" and e.role:
            latest[(e.payload.get("lane", ""), e.role)] = list(e.payload.get("questions") or [])
    return [(role, q) for (_, role), qs in latest.items() for q in qs]


def create_app(service: MissionService, token: str) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async def sync_jira_forever() -> None:
            while True:
                if service.jira_connected():
                    try:
                        await anyio.to_thread.run_sync(service.sync_jira)
                    except Exception as error:  # a flaky network must not stop the dashboard
                        service.settings.put("jira.error", str(error))
                    else:
                        service.settings.put("jira.error", "")
                await anyio.sleep(JIRA_SYNC_SECONDS)

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(sync_jira_forever)
            yield
            tasks.cancel_scope.cancel()

    app = FastAPI(title="codec-swarm", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.add_extension("jinja2.ext.loopcontrols")

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

    def kpis(ms: list[MissionView]) -> dict[str, Any]:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        cost_today = sum(e.payload.get("cost_usd") or 0.0 for e in service.events.list() if e.kind == "cost" and e.created_at.startswith(today))
        lanes = [lane for m in ms for lane in m.lanes.values() if lane.verdicts]
        approved = [lane for lane in lanes if lane.verdicts[-1].get("band") == "approve"]
        return {
            "in_progress": sum(m.stage not in ("pr_ready",) for m in ms),
            "waiting": sum(len(m.open_gates) for m in ms),
            "agents": sum(1 for m in ms for lane in m.lanes.values() if lane.status == "running") + sum(1 for m in ms if m.planning_role),
            "approved": len(approved),
            "judged": len(lanes),
            "cost_today": cost_today,
        }

    def agent_cards(ms: list[MissionView]) -> dict[str, list[dict[str, Any]]]:
        """One card per active lane, in its current role's column; anything at a gate waits on me."""
        cards: dict[str, list[dict[str, Any]]] = {key: [] for key, _ in AGENTS} | {"waiting": []}
        for m in ms:
            if m.planning_gate:
                cards["waiting"].append({"m": m, "lane": "", "gate": m.planning_gate})
            elif m.planning_role in cards:
                cards[m.planning_role].append({"m": m, "lane": "planning", "gate": None})
            for repo, lane in m.lanes.items():
                if lane.gate:
                    cards["waiting"].append({"m": m, "lane": repo, "gate": lane.gate})
                elif lane.status == "running" and lane.current_role in cards:
                    cards[lane.current_role].append({"m": m, "lane": repo, "gate": None})
        return cards

    def last_id() -> int:
        rows = service.events.list()
        return rows[-1].id if rows else 0

    def render(request: Request, name: str, **context: Any) -> HTMLResponse:
        css_version = int((HERE / "static" / "codec.css").stat().st_mtime)  # a changed stylesheet is never served from cache
        jev_on = jev_available() and service.settings.orchestration().jev_enabled
        return templates.TemplateResponse(request, name, {"last_id": last_id(), "jev_on": jev_on, "css_v": css_version, **context})

    async def in_background(ticket: str, work: Callable[[], Awaitable[Any]]) -> None:
        try:
            await work()
        except Exception as error:  # surface failures on the page instead of losing them in a server log
            service.events.append(ticket, "mission.error", {"error": f"{type(error).__name__}: {error}"})

    # --- pages ----------------------------------------------------------------

    def board_context(view: str) -> dict[str, Any]:
        ms = missions()
        return {"stages": STAGES, "agents": AGENTS, "missions": ms, "view": view, "kpis": kpis(ms), "cards": agent_cards(ms), **jira_context()}

    def jira_context() -> dict[str, Any]:
        jira = service.settings.jira()
        return {
            "intake": service.intake(), "jira": jira, "jira_connected": service.jira_connected(),
            "jira_synced": (service.settings.get("jira.synced_at") or "")[11:16], "jira_error": service.settings.get("jira.error") or "",
        }

    @app.get("/", response_class=HTMLResponse)
    async def board(request: Request, view: str = "stage"):
        return render(request, "board.html", **board_context(view))

    @app.get("/partials/board", response_class=HTMLResponse)
    async def board_partial(request: Request, view: str = "stage"):
        return render(request, "_board.html", **board_context(view))

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
        stage_index = {key: i for i, (key, _, _) in enumerate(s for s in STAGES if s[0] != "intake")}
        chat = service.chat.list(ticket)
        groups = []  # (lane key, heading, [(role, lines, stats, working, messages, attach command)])
        sources = [("", "planning", view.planning_roles, view.planning_role, view.planning_role is not None)]
        sources += [(repo, repo, lane.roles, lane.current_role, lane.status == "running") for repo, lane in view.lanes.items()]
        try:
            pack = load_pack(view.pack).pack if view.pack else None
        except (FileNotFoundError, ValueError):  # a pack renamed since the mission ran
            pack = None
        for key, heading, stats, current, running in sources:
            terminals = _terminals(events, key)
            planned = (pack.planning_roles if key == "" else pack.lane_roles) if pack else []
            roles = list(dict.fromkeys([*planned, *terminals, *([current] if current else [])]))
            if key == "" and not roles:
                continue
            groups.append((key, heading, [
                (role, terminals.get(role, []), stats.get(role), running and current == role,
                 [c for c in chat if c.lane == key and c.role == role], service.attach_command(ticket, key, role))
                for role in roles
            ]))
        stages = [s for s in STAGES if s[0] != "intake"]
        return {"m": view, "activity": activity, "groups": groups, "stages": stages, "stage_index": stage_index}

    @app.get("/missions/new", response_class=HTMLResponse)
    async def new_mission(request: Request, ticket: str = "", title: str = "", description: str = ""):
        return render(request, "new_mission.html", prefill={"ticket": ticket, "title": title, "description": description})

    @app.get("/missions/{ticket}", response_class=HTMLResponse)
    async def mission(request: Request, ticket: str):
        if not service.events.list(ticket):
            return PlainTextResponse(f"No mission {ticket}", status_code=404)
        return render(request, "mission.html", **mission_context(ticket))

    @app.get("/partials/missions/{ticket}", response_class=HTMLResponse)
    async def mission_partial(request: Request, ticket: str):
        return render(request, "_mission.html", **mission_context(ticket))

    @app.get("/harness", response_class=HTMLResponse)
    async def harness(request: Request, saved: str = "", error: str = ""):
        packs = [load_pack(name) for name in PACKS]
        overrides = {(p.pack.name, role): service.settings.role_override(p.pack.name, role) for p in packs for role in p.roles}
        return render(request, "harness.html", packs=packs, overrides=overrides, models=MODEL_ORDER, saved=saved, error=error)

    @app.get("/orchestration", response_class=HTMLResponse)
    async def orchestration(request: Request, saved: str = "", error: str = ""):
        o = service.settings.orchestration()
        return render(
            request, "orchestration.html", bands=JudgeBands(), jev_model=JEV_MODEL, o=o, jev_key=jev_available(),
            gate_run=o.gate_threshold + o.gate_margin, gate_unsure=o.gate_threshold - o.gate_margin, saved=saved, error=error,
        )

    @app.get("/requests", response_class=HTMLResponse)
    async def my_requests(request: Request):
        rows = [(m, request_step(m), open_questions(service.events.list(m.ticket))) for m in missions()]
        return render(request, "requests.html", rows=rows, steps=REQUEST_STEPS)

    @app.get("/jira", response_class=HTMLResponse)
    async def jira_page(request: Request, error: str = "", who: str = ""):
        return render(request, "jira.html", error=error, who=who, **jira_context())

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

    @app.post("/harness/{pack}/{role}")
    async def save_role(pack: str, role: str, model: str = Form(""), extra_mcp: list[str] = Form([]), extra_skills: str = Form("")):
        if pack not in PACKS or role not in (definition := load_pack(pack)).roles:
            return PlainTextResponse(f"No role {role} in pack {pack}", status_code=404)
        spec = definition.roles[role]
        if model and (model not in MODEL_ORDER or (spec.min_model and MODEL_ORDER.index(model) < MODEL_ORDER.index(spec.min_model))):
            return RedirectResponse(f"/harness?{urlencode({'error': f'{role} cannot run below {spec.min_model}'})}", status_code=303)
        unknown = [m for m in extra_mcp if m not in definition.mcp_catalog]
        if unknown:
            return RedirectResponse(f"/harness?{urlencode({'error': 'not in the catalog: ' + ', '.join(unknown)})}", status_code=303)
        skills = [s.strip() for s in extra_skills.replace("\n", ",").split(",") if s.strip()]
        override = RoleOverride(model=model or None, extra_mcp=[m for m in extra_mcp if m not in spec.mcp], extra_skills=[s for s in skills if s not in spec.skills])
        service.settings.set_role_override(pack, role, override)
        return RedirectResponse(f"/harness?saved={pack}.{role}#{pack}-{role}", status_code=303)

    @app.post("/orchestration")
    async def save_orchestration(jev_enabled: str = Form(""), gate_threshold: float = Form(...), gate_margin: float = Form(...)):
        if not (0.5 <= gate_threshold <= 0.99 and 0 <= gate_margin <= 0.10 and gate_threshold + gate_margin < 1):
            return RedirectResponse("/orchestration?error=The threshold must be 0.50–0.99, the margin 0–0.10, and together below 1.", status_code=303)
        service.settings.set_orchestration(Orchestration(jev_enabled=bool(jev_enabled), gate_threshold=gate_threshold, gate_margin=gate_margin))
        return RedirectResponse("/orchestration?saved=1", status_code=303)

    @app.post("/missions/{ticket}/chat")
    async def send_chat(ticket: str, role: str = Form(...), lane: str = Form(""), text: str = Form(...)):
        if text.strip():
            service.send_message(ticket, lane, role, text.strip())
        return RedirectResponse(f"/missions/{ticket}?tab=agents", status_code=303)

    @app.post("/missions/{ticket}/attach")
    async def open_terminal(ticket: str, role: str = Form(...), lane: str = Form("")):
        command = service.attach_command(ticket, lane, role)
        if command is None:
            return PlainTextResponse(f"{role} has no session yet", status_code=404)
        if sys.platform != "darwin":
            return PlainTextResponse("Open in Terminal works on macOS; copy the command instead.", status_code=501)
        script = service.root / "attach" / f"{ticket}-{lane or 'planning'}-{role}.command"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(f"#!/bin/zsh\n{command}\n")
        script.chmod(0o700)
        subprocess.run(["open", "-a", "Terminal", str(script)], check=False)
        return RedirectResponse(f"/missions/{ticket}?tab=agents", status_code=303)

    @app.post("/jira/connect")
    async def jira_connect(site: str = Form(...), email: str = Form(...), token: str = Form(...), jql: str = Form("")):
        try:
            who = await anyio.to_thread.run_sync(lambda: service.connect_jira(site, email, token, jql))
        except (JiraError, ValueError) as error:
            return RedirectResponse(f"/jira?{urlencode({'error': str(error)})}", status_code=303)
        return RedirectResponse(f"/jira?{urlencode({'who': who})}", status_code=303)

    @app.post("/jira/sync")
    async def jira_sync(back: str = Form("/")):
        try:
            await anyio.to_thread.run_sync(service.sync_jira)
            service.settings.put("jira.error", "")
        except JiraError as error:
            service.settings.put("jira.error", str(error))
        return RedirectResponse(back if back.startswith("/") else "/", status_code=303)

    @app.post("/jira/jql")
    async def jira_jql(jql: str = Form(...)):
        service.set_jql(jql)
        return await jira_sync("/jira")

    @app.post("/jira/disconnect")
    async def jira_disconnect():
        service.disconnect_jira()
        return RedirectResponse("/jira", status_code=303)

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
