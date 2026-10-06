"""The dashboard: FastAPI + Jinja + htmx. Reads come from the event log; actions go through the mission service."""

from __future__ import annotations

import json
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
from markdown_it import MarkdownIt
from markupsafe import Markup
import yaml
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
from codec_swarm.dispatch import execute
from codec_swarm.service import MissionRequest, MissionService
from codec_swarm.store.commands import CommandQueue
from codec_swarm.workspace.envs import parse as parse_env
from codec_swarm.workspace.repos import check_name, repo_name
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
        note = f" · “{p['note'][:160]}”" if p.get("note") else ""
        return f"{lane}{p.get('kind')} gate: {p.get('answer')}{note}"
    if e.kind == "mission.restarted":
        return "mission restarted from the first role; earlier work was set aside"
    if e.kind == "lane.restarted":
        return f"{lane}lane restarted from its first role" + (f" · “{p['note'][:160]}”" if p.get("note") else "")
    if e.kind == "commit.rejected":
        first = next((line for line in (p.get("output") or "").splitlines() if "✖" in line or "error" in line.lower()), "")
        return f"{lane}{who}: the repo's hooks rejected the commit (try {p.get('tries')}){': ' + first.strip() if first else ''}; sent back to {who}"
    if e.kind == "pr.failed":
        return f"{lane}{p.get('action')} failed: {p.get('error')}"
    if e.kind == "pr.pushed":
        return f"{lane}pushed and opened on GitHub into {p.get('target')}: {p.get('url')}"
    if e.kind == "pr.merged":
        return f"{lane}merged locally into {p.get('target')} ({str(p.get('sha'))[:8]})"
    if e.kind == "mission.interrupted":
        return "the worker restarted mid-command; resuming from the last checkpoint"
    if e.kind == "lane.update":  # one line per update from base: up to date, rebased and checked, or conflicting
        return f"{lane}{p.get('message')}"
    if e.kind == "env.note":
        return f"{lane}environment: {p.get('note')}"
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


def notification(e: Event) -> dict[str, str] | None:
    """What deserves a sound: anything waiting on me, and a lane whose PR is ready."""
    lane = e.payload.get("lane") or ""
    where = f"{e.mission}{' · ' + lane if lane else ''}"
    if e.kind == "gate.opened":
        kind = e.payload.get("kind", "")
        what = {"spec": "the spec is ready for your OK", "pr": "the local PR is ready for review", "review": "the judge wants your review", "handoff": "a step needs you"}.get(kind, f"{kind} gate")
        return {"kind": "input", "title": where, "body": what, "url": f"/missions/{e.mission}/prs/{lane}" if kind in ("pr", "review") and lane else "/inbox"}
    if e.kind in ("mission.blocked", "mission.error", "lane.failed"):
        return {"kind": "input", "title": where, "body": "stopped: " + str(e.payload.get("reason") or e.payload.get("error") or "needs a look")[:140], "url": f"/missions/{e.mission}"}
    if e.kind == "pr.failed":
        return {"kind": "input", "title": where, "body": f"{e.payload.get('action')} failed", "url": f"/missions/{e.mission}/prs/{lane}"}
    if e.kind == "lane.conflict" and not e.payload.get("sent_to"):
        return {"kind": "input", "title": where, "body": "conflicts with its base branch", "url": f"/missions/{e.mission}"}
    if e.kind == "lane.rechecked" and not e.payload.get("passed"):
        return {"kind": "input", "title": where, "body": "checks fail after the update from base", "url": f"/missions/{e.mission}"}
    if e.kind == "mission.done" and e.payload.get("pr_url"):
        return {"kind": "done", "title": where, "body": "PR ready", "url": e.payload["pr_url"]}
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


def create_app(service: MissionService, token: str, queue: CommandQueue | None = None) -> FastAPI:
    """With a queue, every mission action becomes a command for the worker process; without one (tests),
    the same commands run in this process after the response."""
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
    # Agent-written text: raw HTML stays escaped, except the <details> blocks the PR body itself adds.
    md = MarkdownIt("commonmark", {"html": False}).enable("table")

    def markdown(text: str) -> Markup:
        html = md.render(text or "")
        for tag in ("<details>", "</details>", "<summary>", "</summary>", "<code>", "</code>"):
            html = html.replace(tag.replace("<", "&lt;").replace(">", "&gt;"), tag)
        return Markup(html)

    templates.env.filters["markdown"] = markdown

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
        # A changed stylesheet or script is never served from cache.
        css_version = int(max((HERE / "static" / name).stat().st_mtime for name in ("codec.css", "codec.js")))
        jev_on = jev_available() and service.settings.orchestration().jev_enabled
        worker = None if queue is None else {"alive": queue.worker_alive(), "pending": queue.pending_count()}
        return templates.TemplateResponse(request, name, {"last_id": last_id(), "jev_on": jev_on, "css_v": css_version, "worker": worker, **context})

    def dispatch(background: BackgroundTasks, ticket: str, kind: str, **args: Any) -> None:
        if queue is not None:
            queue.submit(ticket, kind, args)
            return

        async def run() -> None:
            try:
                await execute(service, ticket, kind, args)
            except Exception:  # execute() already wrote it to the event log, where the page shows it
                pass

        background.add_task(run)

    def busy(ticket: str) -> bool:
        return service.is_busy(ticket) or (queue is not None and queue.is_busy(ticket))

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
        return {"m": view, "activity": activity, "groups": groups, "stages": stages, "stage_index": stage_index, "busy": busy(ticket)}

    @app.get("/missions/new", response_class=HTMLResponse)
    async def new_mission(request: Request, ticket: str = "", title: str = "", description: str = "", repo_urls: str = ""):
        prefill = {"ticket": ticket, "title": title, "description": description, "repo_urls": repo_urls}
        return render(request, "new_mission.html", prefill=prefill, rows=base_rows(repo_urls), error="")

    def base_rows(repo_urls: str, chosen: dict[str, str] | None = None) -> list[dict[str, Any]]:
        rows = []
        for url in dict.fromkeys(u.strip() for u in repo_urls.splitlines() if u.strip()):
            try:
                name = check_name(repo_name(url))
            except ValueError:
                continue
            configured = service.repos.configured_base(name)
            rows.append({
                "name": name, "configured": configured, "branches": service.repos.branches(name),
                "value": (chosen or {}).get(name) or configured or "",
                "env_keys": [v.key for v in service.envs.masked(name)], "env_file": service.env_file(name),
            })
        return rows

    @app.get("/partials/bases", response_class=HTMLResponse)
    async def bases_partial(request: Request, repo_urls: str = ""):
        return render(request, "_bases.html", rows=base_rows(repo_urls))

    @app.get("/missions/{ticket}", response_class=HTMLResponse)
    async def mission(request: Request, ticket: str, error: str = "", notice: str = ""):
        if not service.events.list(ticket):
            return PlainTextResponse(f"No mission {ticket}", status_code=404)
        return render(request, "mission.html", error=error, notice=notice, **mission_context(ticket))

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

    def repo_missions() -> dict[str, list[str]]:
        used: dict[str, list[str]] = {}
        for e in service.events.list():
            if e.kind == "mission.started":
                for repo in e.payload.get("repos") or []:
                    used.setdefault(repo, []).append(e.mission)
        return used

    @app.get("/repos", response_class=HTMLResponse)
    async def repos(request: Request, error: str = ""):
        rows = [service.repos.summary(name) for name in service.repos.names()]
        return render(request, "repos.html", rows=rows, used=repo_missions(), error=error)

    @app.get("/repos/{name}", response_class=HTMLResponse)
    async def repo_page(request: Request, name: str, saved: str = "", env: str = "", env_error: str = ""):
        try:
            detail = service.repos.detail(name)
        except ValueError:
            return PlainTextResponse(f"No repo {name}", status_code=404)
        if not detail.cloned and not detail.local_file:
            return PlainTextResponse(f"No repo {name}", status_code=404)
        return render_repo(request, detail, saved=bool(saved), error=env_error, env_note=env)

    def render_repo(
        request: Request, detail: Any, error: str = "", config_text: str | None = None, domain_text: str | None = None,
        status: int = 200, saved: bool = False, env_note: str = "",
    ) -> HTMLResponse:
        text = config_text if config_text is not None else (detail.local_text or ("" if detail.repo_file else service.repos.starter()))
        sensitive = bool(detail.config and detail.config.sensitive)
        response = render(
            request, "repo.html", r=detail, error=error, config_text=text, saved=saved,
            env_vars=service.envs.masked(detail.name, sensitive), env_file=service.env_file(detail.name), sensitive=sensitive, env_note=env_note,
            domain_text=domain_text if domain_text is not None else detail.domain_text,
            stacks=service.repos.stacks(), used=repo_missions().get(detail.name, []),
            config_yaml=yaml.safe_dump(detail.config.model_dump(mode="json", exclude={"domain"}), sort_keys=False) if detail.config else "",
        )
        response.status_code = status
        return response

    @app.get("/jira", response_class=HTMLResponse)
    async def jira_page(request: Request, error: str = "", who: str = ""):
        return render(request, "jira.html", error=error, who=who, **jira_context())

    # --- actions --------------------------------------------------------------

    @app.post("/missions")
    async def start_mission(
        request_: Request,
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
        form = await request_.form()
        chosen = {k.removeprefix("base:"): str(v).strip() for k, v in form.items() if k.startswith("base:") and str(v).strip()}
        env_text = {k.removeprefix("env:"): str(v) for k, v in form.items() if k.startswith("env:") and str(v).strip()}
        if form.get("restart"):
            service.envs.drop_overrides(ticket.strip())  # a restart with changes takes only the overrides in this form
        rows = base_rows(repo_urls, chosen)
        unknown = [f"{r['name']} has no branch {r['value']!r} on origin" for r in rows if r["branches"] and r["value"] and r["value"] not in r["branches"]]
        for repo, text in env_text.items():
            try:
                parse_env(text)
            except ValueError as error:
                unknown.append(f"{repo} overrides: {error}")
        if unknown:
            prefill = {"ticket": ticket, "title": title, "description": description, "repo_urls": repo_urls}
            response = render(request_, "new_mission.html", prefill=prefill, rows=rows, error="; ".join(unknown))
            response.status_code = 400
            return response
        request = MissionRequest(
            ticket=ticket.strip(), title=title.strip(), description=description.strip(),
            repo_urls=tuple(u.strip() for u in repo_urls.splitlines() if u.strip()),
            autonomy=Autonomy(autonomy), pack=pack, model=model.strip() or None, no_jev=not jev,
            bases={r["name"]: r["value"] for r in rows if r["value"] and r["value"] != r["configured"]},
            env_overrides={repo: service.envs.set_overrides(ticket.strip(), repo, text) for repo, text in env_text.items() if repo in {r["name"] for r in rows}},
        )
        if form.get("restart"):
            if (refused := busy_redirect(request.ticket)) is not None:
                return refused
            dispatch(background, request.ticket, "restart", request=request.model_dump(mode="json"))
        else:
            dispatch(background, request.ticket, "start", request=request.model_dump(mode="json"))
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

    @app.get("/missions/{ticket}/prs/{repo}", response_class=HTMLResponse)
    async def local_pr(request: Request, ticket: str, repo: str, error: str = "", done: str = "", target: str = ""):
        loaded = service.local_pr(ticket, repo)
        if loaded is None:
            return PlainTextResponse(f"{ticket} has no local PR for {repo} yet", status_code=404)
        pr, body = loaded
        events = service.events.list(ticket)
        view = mission_view(events)
        lane = view.lanes.get(repo)
        failure = None  # the worker's last push or merge failure, unless one succeeded since
        for e in events:
            if e.payload.get("lane") == repo and e.kind in ("pr.pushed", "pr.merged", "pr.failed"):
                failure = e.payload.get("error") if e.kind == "pr.failed" else None
        error = error or failure or ""
        stat, diff = await anyio.to_thread.run_sync(service.prs.diff, pr)
        verdicts = lane.verdicts if lane else []
        return render(
            request, "pr.html", m=view, pr=pr, body=body, lane=lane, stat=stat, diff_lines=diff.splitlines(),
            branches=service.prs.branches(pr), verdict=verdicts[-1] if verdicts else None, error=error, done=done, target=target,
            gate=lane.gate if lane and lane.gate and lane.gate.kind in ("pr", "review") else None, busy=busy(ticket),
        )

    @app.post("/missions/{ticket}/prs/{repo}")
    async def pr_action(background: BackgroundTasks, ticket: str, repo: str, action: str = Form(...), target: str = Form(...)):
        back = f"/missions/{ticket}/prs/{repo}"
        problem = None
        if action not in ("push", "merge"):
            problem = f"unknown action {action}"
        elif service.local_pr(ticket, repo) is None:
            problem = f"{ticket} has no local PR for {repo}"
        elif subprocess.run(["git", "check-ref-format", "--branch", target.strip()], capture_output=True).returncode != 0:
            problem = f"{target!r} is not a branch name"
        elif busy(ticket):
            problem = f"{ticket} is working right now; try again once it waits here"
        if problem:
            return RedirectResponse(f"{back}?{urlencode({'error': problem, 'target': target})}", status_code=303)
        dispatch(background, ticket, "pr_action", repo=repo, action=action, target=target.strip())
        return RedirectResponse(f"{back}?{urlencode({'done': action, 'target': target})}", status_code=303)

    def busy_redirect(ticket: str) -> RedirectResponse | None:
        if busy(ticket):
            message = f"{ticket} is working right now; try again once it waits at a gate or stops."
            return RedirectResponse(f"/missions/{ticket}?{urlencode({'error': message})}", status_code=303)
        return None

    @app.post("/missions/{ticket}/resume")
    async def resume_mission(background: BackgroundTasks, ticket: str):
        if (refused := busy_redirect(ticket)) is not None:
            return refused
        dispatch(background, ticket, "recover")
        return RedirectResponse(f"/missions/{ticket}?{urlencode({'notice': 'Resuming from the last checkpoint.'})}", status_code=303)

    @app.post("/missions/{ticket}/restart")
    async def restart_mission(background: BackgroundTasks, ticket: str):
        if (refused := busy_redirect(ticket)) is not None:
            return refused
        dispatch(background, ticket, "restart")
        return RedirectResponse(f"/missions/{ticket}?{urlencode({'notice': 'Restarting from the first role.'})}", status_code=303)

    @app.get("/missions/{ticket}/restart", response_class=HTMLResponse)
    async def restart_form(request: Request, ticket: str):
        stored = service.stored_request(ticket)
        if stored is None:
            return PlainTextResponse(f"No mission {ticket}", status_code=404)
        r = stored[0]
        prefill = {"ticket": r.ticket, "title": r.title, "description": r.description, "repo_urls": "\n".join(r.repo_urls),
                   "autonomy": r.autonomy.value, "pack": r.pack, "model": r.model or "", "restart": True}
        return render(request, "new_mission.html", prefill=prefill, rows=base_rows(prefill["repo_urls"], r.bases), error="")

    @app.post("/missions/{ticket}/lanes/{repo}/restart")
    async def restart_lane(background: BackgroundTasks, ticket: str, repo: str, note: str = Form("")):
        if (refused := busy_redirect(ticket)) is not None:
            return refused
        dispatch(background, ticket, "restart_lane", repo=repo, note=note)
        return RedirectResponse(f"/missions/{ticket}?{urlencode({'notice': f'Restarting the {repo} lane from its first role.'})}", status_code=303)

    @app.post("/missions/{ticket}/lanes/{repo}/update")
    async def update_lane(background: BackgroundTasks, ticket: str, repo: str, resolve: str = Form(""), back: str = Form("")):
        if (refused := busy_redirect(ticket)) is not None:
            return refused
        dispatch(background, ticket, "update", repo=repo, resolve=bool(resolve))
        target = back if back.startswith("/") else f"/missions/{ticket}"
        return RedirectResponse(f"{target}?{urlencode({'notice': f'Updating {repo} from its base; the result shows in Activity.'})}", status_code=303)

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

    @app.post("/repos")
    async def add_repo(url: str = Form(...)):
        try:
            name = await anyio.to_thread.run_sync(service.repos.add, url)
        except (ValueError, RuntimeError) as error:
            return RedirectResponse(f"/repos?{urlencode({'error': str(error)})}", status_code=303)
        return RedirectResponse(f"/repos/{name}", status_code=303)

    @app.post("/repos/{name}/env")
    async def repo_env(name: str, op: str = Form(...), key: str = Form(""), value: str = Form(""), text: str = Form(""), filename: str = Form("")):
        back = f"/repos/{name}"
        try:
            check_name(name)
            if op == "set":
                service.envs.set(name, key.strip(), value)
                note = f"Saved {key.strip()}."
            elif op == "import":
                names = service.envs.merge(name, text)
                note = f"Imported {len(names)} variable{'s' if len(names) != 1 else ''}."
            elif op == "delete":
                service.envs.delete(name, key)
                note = f"Deleted {key}."
            elif op == "file":
                service.set_env_file(name, filename)
                note = f"Lanes get {service.env_file(name)}."
            else:
                raise ValueError(f"unknown action {op}")
        except ValueError as error:
            return RedirectResponse(f"{back}?{urlencode({'env_error': str(error)})}#env", status_code=303)
        return RedirectResponse(f"{back}?{urlencode({'env': note})}#env", status_code=303)

    @app.post("/repos/{name}/config")
    async def save_repo(request: Request, name: str, config: str = Form(""), domain: str = Form("")):
        try:
            service.repos.save(name, config, domain)
        except ValueError as error:  # pydantic's ValidationError is a ValueError too
            return render_repo(request, service.repos.detail(name), error=str(error), config_text=config, domain_text=domain, status=400)
        return RedirectResponse(f"/repos/{name}?saved=1", status_code=303)

    @app.post("/missions/{ticket}/gates")
    async def answer_gate(
        background: BackgroundTasks, ticket: str, lane: str = Form(""), answer: str = Form(...), back: str = Form(""), note: str = Form(""),
    ):
        note = note.strip() if answer == "send_back" else ""
        dispatch(background, ticket, "answer", lane=lane, answer=answer, note=note)
        return RedirectResponse(back or f"/missions/{ticket}", status_code=303)

    # --- live feed --------------------------------------------------------------

    @app.get("/events/stream")
    async def stream(request: Request, since: int = 0, once: bool = False):
        # A reconnecting EventSource sends the last id it saw; resume there so no note (or sound) repeats.
        resumed = request.headers.get("last-event-id", "")
        start = int(resumed) if resumed.isdigit() else since

        async def changes():
            last = start
            while True:
                rows = service.events.list(since=last)
                if rows:
                    last = rows[-1].id
                    for row in rows:
                        if (note := notification(row)) is not None:
                            yield {"event": "notify", "data": json.dumps(note), "id": str(row.id)}
                    yield {"event": "change", "data": str(last), "id": str(last)}
                    if once:
                        return
                elif once:
                    return
                await anyio.sleep(1)

        return EventSourceResponse(changes())

    return app
