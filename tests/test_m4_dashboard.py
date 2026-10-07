"""Step definitions for features/m4_dashboard.feature: the web app over a fake mission service."""

import os
import re
import sqlite3
import stat

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from codec_swarm.domain import CODEC_STANDARD, Autonomy
from codec_swarm.graph import APPROVE, MissionRunner
from codec_swarm.graph.coordinator import MissionCoordinator
from codec_swarm.plugins.fake import FakeBackend, FakeJudge
from codec_swarm.plugins.fallback import PackOrderRouter
from codec_swarm.plugins.jev import JevClient
from codec_swarm.service import MissionRequest, MissionRuntime, MissionService, repo_name
from codec_swarm.domain import Mission
from codec_swarm.harness import load_pack, resolve_session
from codec_swarm.harness.config import RepoConfig
from codec_swarm.web.app import create_app

scenarios("../features/m4_dashboard.feature")


class FakeMissionService(MissionService):
    """The real service's start/answer/recover, with missions built from the fake backend and judge."""

    async def build(self, request: MissionRequest, pack_name: str | None = None) -> MissionRuntime:
        names = tuple(repo_name(u) for u in request.repo_urls)
        mission = Mission(ticket=request.ticket, repo="", repos=names, title=request.title, description=request.description, autonomy=request.autonomy)
        backend, judge = FakeBackend(**getattr(self, "backend_options", {})), FakeJudge()
        self.backend = backend

        def runner(part):
            return MissionRunner(self.db, CODEC_STANDARD, backend, PackOrderRouter(), judge, self.events, part=part, chat=self.chat)

        coordinator = MissionCoordinator(runner("planning"), runner("lane"), self.events)
        runtime = MissionRuntime(request, mission, "codec-standard", False, coordinator, JevClient())
        self._runtimes[request.ticket] = runtime
        return runtime


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)  # a developer's own token must not leak into these tests
    yield {"tmp": tmp_path}
    os.environ.pop("JIRA_API_TOKEN", None)


@given(parsers.parse('the dashboard runs with a fake mission service and launch token "{token}"'))
def dashboard(web, token):
    web["service"] = FakeMissionService(web["tmp"] / "root")
    web["app"] = create_app(web["service"], token)
    web["client"] = TestClient(web["app"])
    web["token"] = token


def _request(ticket, title, repo, autonomy="gated"):
    return MissionRequest(ticket=ticket, title=title, repo_urls=(repo,), autonomy=Autonomy(autonomy), no_jev=True)


@given("I am signed in")
def signed_in(web):
    web["client"].get(f"/?token={web['token']}")


@given(parsers.parse('mission {ticket} "{title}" on {repo} is waiting at its {kind} gate'))
def waiting_mission(web, ticket, title, repo, kind):
    anyio.run(web["service"].start, _request(ticket, title, repo))
    if kind == "pr":
        anyio.run(web["service"].answer, ticket, None, APPROVE)


@when(parsers.parse('I open "{path}" without the token'))
def open_without_token(web, path):
    web["response"] = TestClient(web["app"]).get(path, follow_redirects=False)


@when(parsers.parse('I open "{path}"'))
def open_path(web, path):
    web["response"] = web["client"].get(path, follow_redirects=False)


@then(parsers.parse("the response is {code:d}"))
def response_code(web, code):
    assert web["response"].status_code == code


@then(parsers.parse('I am redirected to "{path}" with a session cookie'))
def redirected(web, path):
    assert web["response"].status_code == 303 and web["response"].headers["location"] == path
    assert web["client"].cookies.get("codec_session") == web["token"]


@then('opening "/" with that cookie shows the mission board')
def board_with_cookie(web):
    page = web["client"].get("/")
    assert page.status_code == 200 and "<h1>Missions</h1>" in page.text


@when(parsers.parse('I start mission {ticket} "{title}" on {repo} in {autonomy} mode'))
def start_from_form(web, ticket, title, repo, autonomy):
    form = {"ticket": ticket, "title": title, "repo_urls": repo, "autonomy": autonomy, "pack": "codec-standard"}
    web["response"] = web["client"].post("/missions", data=form, follow_redirects=False)
    assert web["response"].status_code == 303


def _board_stage(web, ticket):
    html = web["client"].get("/").text
    match = re.search(rf'data-ticket="{ticket}" data-stage="(\w+)"', html)
    return match.group(1) if match else None


@then(parsers.parse("the mission board lists {ticket} under {stage}"))
def board_lists(web, ticket, stage):
    assert _board_stage(web, ticket) == stage.replace(" ", "_")


@then(parsers.parse("the inbox shows the {kind} gate of {ticket} for lane {lane}"))
def inbox_lane_gate(web, kind, ticket, lane):
    assert f'data-gate="{ticket}:{lane}:{kind}"' in web["client"].get("/inbox").text


@then(parsers.parse("the inbox shows the {kind} gate of {ticket}"))
def inbox_gate(web, kind, ticket):
    assert f'data-gate="{ticket}::{kind}"' in web["client"].get("/inbox").text


@when(parsers.parse("I approve the spec gate of {ticket} from the inbox"))
def approve_spec(web, ticket):
    web["client"].post(f"/missions/{ticket}/gates", data={"lane": "", "answer": "approve", "back": "/inbox"})


@when(parsers.parse("I approve the pr gate of {ticket} for lane {lane}"))
def approve_lane(web, ticket, lane):
    web["client"].post(f"/missions/{ticket}/gates", data={"lane": lane, "answer": "approve", "back": "/inbox"})


@then("the inbox is empty")
def inbox_empty(web):
    assert "Nothing is waiting on you." in web["client"].get("/inbox").text


@when(parsers.parse("I open the mission page of {ticket}"))
def mission_page(web, ticket):
    web["page"] = web["client"].get(f"/missions/{ticket}").text


def _section(web, marker, end):
    html = web["page"]
    start = html.index(marker)
    return html[start : html.index(end, start)]


@then(parsers.parse("it shows lane {repo} waiting at the pr gate"))
def lane_waiting(web, repo):
    row = _section(web, f'class="lanerow" data-lane="{repo}"', "</div>\n  </div>")
    assert 'data-status="waiting"' in row and "pr gate" in row


@then(parsers.parse("it shows the judge's verdict for lane {repo}"))
def lane_verdict(web, repo):
    dod = _section(web, f'data-dod="{repo}"', "</section>")
    assert "data-verdict" in dod and "approve band" in dod


@then(parsers.parse("it shows the agent output of lane {repo}"))
def lane_output(web, repo):
    terminal = _section(web, f'data-lane="{repo}" data-role="backend-coder"', "</section>")
    assert "› backend-coder working on" in terminal


@then(parsers.parse("the Waiting on you column holds lane {lane} of {ticket}"))
def waiting_column(web, lane, ticket):
    html = web["response"].text
    column = html[html.index('data-agent="waiting"') :]
    assert f'data-ticket="{ticket}" data-lane="{lane}"' in column


@then(parsers.parse("the board shows {count:d} gate waiting on me"))
def kpi_waiting(web, count):
    assert re.search(rf"<span>Waiting on you</span><b>{count}</b>", web["response"].text)


@when("I read the live feed since event 0 once")
def live_feed(web):
    web["feed"] = web["client"].get("/events/stream?since=0&once=true").text


@then("it sends a change event with the latest event id")
def change_event(web):
    last = web["service"].events.list()[-1].id
    assert "event: change" in web["feed"] and f"data: {last}" in web["feed"]


@then("the page lists the specifier with model opus and floor sonnet")
def harness_specifier(web):
    row = re.search(r'data-role="specifier"[^>]*>(.*?)</tr>', web["response"].text, re.S).group(1)
    assert ">opus<" in row and ">sonnet<" in row


@then("the page lists the backend-coder's MCP server context7")
def harness_mcp(web):
    row = re.search(r'data-role="backend-coder"[^>]*>(.*?)</tr>', web["response"].text, re.S).group(1)
    assert "context7" in row


@then("the page shows the judge bands 0.95 and 0.80")
def orchestration_bands(web):
    assert "approve ≥ 0.95 · review ≥ 0.80" in web["response"].text


@then("the page shows the command-gate band from 0.87 to 0.93")
def orchestration_gate(web):
    assert "unsure 0.87–0.93" in web["response"].text


# --- editable harness and orchestration ------------------------------------------


@when(parsers.parse("I set the {role} of {pack} to model {model} with MCP server {server}"))
def set_role(web, role, pack, model, server):
    form = {"model": model, "extra_skills": ""}
    if server != "none":
        form["extra_mcp"] = [server]
    web["response"] = web["client"].post(f"/harness/{pack}/{role}", data=form)


@when(parsers.parse("I set the {role} of {pack} to model {model} with no MCP server"))
def set_role_no_mcp(web, role, pack, model):
    web["response"] = web["client"].post(f"/harness/{pack}/{role}", data={"model": model})


@then(parsers.parse("the harness page shows {role} overridden to {model} with +{server}"))
def harness_override(web, role, model, server):
    row = re.search(rf'data-role="{role}" id="[^"]+">(.*?)</tr>', web["response"].text, re.S).group(1)
    assert f'→ <span class="pill amber">{model}</span>' in row and f"+{server}" in row


@then(parsers.parse("a {role} session resolves to model {model} with servers {first} and {second}"))
def session_resolves(web, role, model, first, second):
    pack = load_pack("codec-standard")
    mission = Mission(ticket="T-1", repo="r", title="t")
    overrides = web["service"].local_overrides("codec-standard", role)
    spec = resolve_session(pack, RepoConfig(stack="python"), role, web["tmp"], mission, overrides=overrides)
    assert spec.model == model and not spec.model_forced
    assert list(spec.mcp_servers) == [first, second]


@then(parsers.parse('the harness page says "{text}"'))
def harness_says(web, text):
    assert text in web["response"].text


@then(parsers.parse("the {role} of {pack} has no override"))
def no_override(web, role, pack):
    assert web["service"].settings.role_override(pack, role).model is None


@when(parsers.parse("I save orchestration with Jev off, threshold {threshold} and margin {margin}"))
def save_orchestration(web, threshold, margin):
    web["response"] = web["client"].post("/orchestration", data={"gate_threshold": threshold, "gate_margin": margin})


@then(parsers.parse("the page shows the command-gate band from {low} to {high}"))
def gate_band(web, low, high):
    assert f"unsure {low}–{high}" in web["response"].text


@then("the orchestration settings say Jev is off")
def jev_off(web):
    assert web["service"].settings.orchestration().jev_enabled is False


# --- chat and attach ------------------------------------------------------------


@when(parsers.parse('I send "{text}" to the {role} of lane {lane} in {ticket}'))
def send_chat(web, text, role, lane, ticket):
    web["chat"] = text
    response = web["client"].post(f"/missions/{ticket}/chat", data={"lane": lane, "role": role, "text": text}, follow_redirects=False)
    assert response.status_code == 303


@then(parsers.parse("the mission page of {ticket} shows that message as {state}"))
def chat_state(web, ticket, state):
    page = web["client"].get(f"/missions/{ticket}").text
    log = page[page.index("data-chat") :]
    assert f"{state}</span> {web['chat']}" in log[: log.index("</ul>")]


@then(parsers.parse('the {role}\'s step received "{text}"'))
def step_received(web, role, text):
    assert (role, (text,)) in web["service"].backend.messages


@given(parsers.parse('the {role} of lane {lane} in {ticket} ran as session "{session}"'))
def ran_as(web, role, lane, ticket, session):
    web["service"]._sessions.record(ticket, lane, role, session, "sonnet", 3, 0.1)


@then(parsers.parse('it shows "{command}" for the {role}'))
def shows_attach(web, command, role):
    panel = _section(web, f'data-role="{role}"', "</section>")
    assert command in panel and "Open in Terminal" in panel


# --- Jira -----------------------------------------------------------------------


@then(parsers.parse("the Intake column shows {key} marked as an example"))
def intake_example(web, key):
    card = re.search(rf'data-intake="{key}">(.*?)</div>\s*</div>', web["response"].text, re.S)
    assert card and "example</span>" in card.group(1)


@then("the Jira bar offers to connect Jira")
def offers_connect(web):
    html = web["response"].text
    assert 'data-jira="examples"' in html and "Connect Jira" in html


def _jira_transport(handler):
    def wrapped(request: httpx.Request) -> httpx.Response:
        return handler(request)

    return httpx.MockTransport(wrapped)


@given(parsers.parse('Jira at "{site}" answers for "{email}" with ticket {key} "{summary}"'))
def jira_answers(web, site, email, key, summary):
    def handler(request):
        assert str(request.url).startswith(site)
        if request.url.path == "/rest/api/3/myself":
            return httpx.Response(200, json={"displayName": "Me"})
        description = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Partial captures must refund."}]}]}
        issue = {"key": key, "fields": {"summary": summary, "status": {"name": "To Do"}, "priority": {"name": "High"}, "description": description}}
        return httpx.Response(200, json={"issues": [issue]})

    web["service"].jira_transport = _jira_transport(handler)


@given(parsers.parse('Jira at "{site}" rejects every token'))
def jira_rejects(web, site):
    web["service"].jira_transport = _jira_transport(lambda request: httpx.Response(401, json={}))


@when(parsers.parse('I connect Jira with site "{site}", email "{email}" and a token'))
def connect_jira(web, site, email):
    web["jira_token"] = "test-token-not-real-123"
    form = {"site": site, "email": email, "token": web["jira_token"], "jql": ""}
    web["response"] = web["client"].post("/jira/connect", data=form)


@then(parsers.parse("the Intake column shows {key} and no example tickets"))
def intake_synced(web, key):
    html = web["client"].get("/").text
    assert f'data-intake="{key}"' in html and 'data-intake="CODEC-901"' not in html


@then("the token is in the root's .env, readable only by me, and nowhere in the database")
def token_stored(web):
    env = web["service"].root / ".env"
    assert web["jira_token"] in env.read_text()
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    db = sqlite3.connect(web["service"].db)
    dump = "\n".join(db.iterdump())
    assert web["jira_token"] not in dump


@then(parsers.parse("the Start mission link for {key} fills in its title"))
def start_link(web, key):
    html = web["client"].get("/").text
    href = re.search(rf'data-intake="{key}".*?class="start" href="([^"]+)"', html, re.S).group(1).replace("&amp;", "&")
    form = web["client"].get(href).text
    assert 'value="Refund partial captures"' in form and "Partial captures must refund." in form


@then(parsers.parse('the Jira page says "{text}"'))
def jira_says(web, text):
    assert text in web["response"].text


@then("Jira is not connected")
def jira_not_connected(web):
    assert not web["service"].jira_connected()
    assert not (web["service"].root / ".env").exists()


# --- my requests ----------------------------------------------------------------


@then(parsers.parse('{ticket} is at the step "{label}"'))
def request_step(web, ticket, label):
    card = re.search(rf'data-request="{ticket}"(.*?)</article>', web["response"].text, re.S).group(1)
    assert f'<li class="now">{label}</li>' in card


@then("it says I need to approve the spec")
def needs_spec(web):
    assert "Waiting for you: approve the spec" in web["response"].text


# --- repos ------------------------------------------------------------------------


def _git(cwd, *args):
    import subprocess

    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@given(parsers.parse('a local origin repo "{name}" with no swarm config'))
def origin_repo(web, name):
    origin = web["tmp"] / "origins" / name
    origin.mkdir(parents=True)
    (origin / "README.md").write_text("billing\n")
    _git(origin, "init", "-q", "-b", "develop")
    _git(origin, "add", ".")
    _git(origin, "-c", "user.name=t", "-c", "user.email=t@l", "commit", "-qm", "init")
    web["origin"], web["repo"] = origin, name


@when("I add that repo on the Repos page")
@given("I added that repo on the Repos page")
def add_repo(web):
    response = web["client"].post("/repos", data={"url": str(web["origin"])}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == f"/repos/{web['repo']}"


@then(parsers.parse("the Repos page lists {name} as needing config"))
def repos_list(web, name):
    row = re.search(rf'data-repo="{name}">(.*?)</tr>', web["client"].get("/repos").text, re.S).group(1)
    assert "needs config" in row


@then(parsers.parse("the repo page for {name} offers a starter local config with stack {stack}"))
def starter(web, name, stack):
    page = web["client"].get(f"/repos/{name}").text
    assert f"stack: {stack}" in page and "Kept on this machine only" in page


@when(parsers.parse('I save {name}\'s local config with stack {stack}, allowlist "{command}" and domain "{domain}"'))
def save_local(web, name, stack, command, domain):
    config = f"stack: {stack}\nallowlist:\n  - {command}\n"
    web["response"] = web["client"].post(f"/repos/{name}/config", data={"config": config, "domain": domain}, follow_redirects=False)
    assert web["response"].status_code == 303


@when(parsers.parse('I save {name}\'s local config as "{text}"'))
def save_raw(web, name, text):
    web["response"] = web["client"].post(f"/repos/{name}/config", data={"config": text, "domain": ""}, follow_redirects=False)


@then(parsers.parse("the repo page for {name} says stack and allowlist come from local"))
def from_local(web, name):
    page = web["client"].get(f"/repos/{name}").text
    for key in ("stack", "allowlist"):
        row = re.search(rf'data-setting="{key}">(.*?)</tr>', page, re.S).group(1)
        assert ">local<" in row


@then(parsers.parse('{name}\'s config in effect allows "{command}" and has the domain "{domain}"'))
def config_in_effect(web, name, command, domain):
    config = web["service"].repos.detail(name).config
    assert config.allowlist == (command,) and config.domain == domain


@then(parsers.parse("the {name} clone has no .swarm files"))
def clone_clean(web, name):
    clone = web["service"].root / "repos" / name
    assert not (clone / ".swarm").exists()
    import subprocess

    assert subprocess.run(["git", "status", "--porcelain"], cwd=clone, capture_output=True, text=True).stdout == ""


@then(parsers.parse('the save is refused with "{text}"'))
def refused(web, text):
    import html

    assert web["response"].status_code == 400 and text in html.unescape(web["response"].text)


@then(parsers.parse("{name} has no local config file"))
def no_local(web, name):
    assert not (web["service"].root / "repos.d" / f"{name}.yaml").exists()


# --- local PRs --------------------------------------------------------------------


@given(parsers.parse('mission {ticket} "{title}" has a local PR for {repo}'))
def has_local_pr(web, ticket, title, repo):
    from codec_swarm.domain import Handoff
    from codec_swarm.harness.config import BranchFlow
    from codec_swarm.workspace import Workspace
    from codec_swarm.workspace.github import GitHubPublisher

    seed = web["tmp"] / "seed"
    seed.mkdir()
    (seed / "README.md").write_text("# payments\n")
    _git(seed, "init", "-q", "-b", "develop")
    _git(seed, "add", ".")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@l", "commit", "-qm", "init")
    _git(web["tmp"], "clone", "-q", "--bare", str(seed), "origin.git")
    service = web["service"]
    workspace = Workspace(service.root)
    lane = workspace.prepare_lane(str(web["tmp"] / "origin.git"), repo, ticket, title, BranchFlow(base="develop"))
    (lane.path / "src").mkdir()
    (lane.path / "src" / "refunds.ts").write_text("export const refund = () => {};\n")
    handoff = Handoff(from_role="backend-coder", summary="done", commit_message="feat: refunds", change_summary="Refunds can now be partial.")
    workspace.record_handoff(lane, 1, handoff, "reviewer")
    mission = Mission(ticket=ticket, repo=repo, title=title)
    url = GitHubPublisher({(ticket, repo): lane}, record_dir=workspace.record_dir).prepare(mission, [handoff], {"source": "checks-only", "band": "approve", "rationale": "unit: pass"})
    request = _request(ticket, title, str(web["tmp"] / "origin.git"))
    service.events.append(ticket, "mission.started", {"repos": [repo], "pack": "codec-standard", "request": request.model_dump(mode="json")})
    service.events.append(ticket, "pr.local", {"lane": repo, "url": url})
    service.events.append(ticket, "mission.done", {"lane": repo, "status": "pr_ready", "pr_url": url})


@when(parsers.parse("I open the local PR of {ticket} for {repo}"))
def open_local_pr(web, ticket, repo):
    web["pr_path"] = f"/missions/{ticket}/prs/{repo}"
    web["page"] = web["client"].get(web["pr_path"]).text


@then(parsers.parse('it shows the description "{text}" and the diff of {path}'))
def pr_shows(web, text, path):
    page = web["page"]
    assert text in page[page.index("data-description") :]
    assert f"+++ b/{path}" in page[page.index("data-diff") :]


@then(parsers.parse("the target branch defaults to {branch}"))
def default_target(web, branch):
    assert f'name="target" list="branches" value="{branch}"' in web["page"]


@when(parsers.parse("I merge it locally into {branch} from the page"))
def merge_from_page(web, branch):
    response = web["client"].post(web["pr_path"], data={"action": "merge", "target": branch}, follow_redirects=False)
    assert response.status_code == 303 and "done=merge" in response.headers["location"], response.headers["location"]


@then(parsers.parse("the local PR says it was merged into {branch}"))
def pr_merged(web, branch):
    page = web["client"].get(web["pr_path"]).text
    assert f"merged into {branch}" in page[page.index("data-merged") :]


@then(parsers.parse("the mission page of {ticket} shows the lane merged into {branch}"))
def mission_merged(web, ticket, branch):
    page = web["client"].get(f"/missions/{ticket}").text
    assert "data-local-pr" in page and f"merged into {branch}" in page


# --- base branch per mission ------------------------------------------------------


@given(parsers.parse('origin "{name}" also has a branch {branch}'))
def origin_branch(web, name, branch):
    _git(web["origin"], "branch", branch)


@given(parsers.parse("I saved {name}'s local config with stack {stack} and base {base}"))
def saved_base(web, name, stack, base):
    config = f"stack: {stack}\nbranch_flow:\n  base: {base}\n"
    assert web["client"].post(f"/repos/{name}/config", data={"config": config, "domain": ""}, follow_redirects=False).status_code == 303


@when(parsers.parse("I list the bases for {name} on the New mission form"))
def list_bases(web, name):
    web["bases"] = web["client"].get("/partials/bases", params={"repo_urls": str(web["origin"])}).text


@then(parsers.parse("{name}'s base defaults to {base} and offers {other}"))
def base_default(web, name, base, other):
    row = re.search(rf'data-base="{name}">(.*?)</label>', web["bases"], re.S).group(1)
    assert f'value="{base}"' in row.split("<datalist")[0] and f'<option value="{other}">' in row


@when(parsers.parse("I start mission {ticket} on {name} from {base}"))
def start_from_base(web, ticket, name, base):
    form = {"ticket": ticket, "title": "Add from_cents", "repo_urls": str(web["origin"]), "pack": "codec-standard", f"base:{name}": base}
    web["response"] = web["client"].post("/missions", data=form, follow_redirects=False)


@then(parsers.parse("mission {ticket} starts {name} from {base}"))
def started_from(web, ticket, name, base):
    assert web["response"].status_code == 303
    request, _ = web["service"].stored_request(ticket)
    assert request.bases == {name: base}


@then(parsers.parse('the form says "{text}"'))
def form_says(web, text):
    import html

    assert web["response"].status_code == 400 and text in html.unescape(web["response"].text)


@then(parsers.parse("no mission {ticket} was started"))
def not_started(web, ticket):
    assert web["service"].events.list(ticket) == []


@then(parsers.parse("it sends an input notification for the {kind} gate of {ticket}"))
def input_note(web, kind, ticket):
    import json

    notes = [json.loads(line.removeprefix("data: ")) for block in web["feed"].split("\r\n\r\n") if "event: notify" in block
             for line in block.splitlines() if line.startswith("data: ")]
    expected = {"spec": "the spec is ready for your OK", "pr": "the local PR is ready for review"}[kind]
    assert any(n["kind"] == "input" and n["title"].startswith(ticket) and n["body"] == expected for n in notes), notes


@when(parsers.parse("I save {name}'s local config with the allowlist indented under branch_flow"))
def misindented(web, name):
    config = "stack: python\nbranch_flow:\n  base: develop\n  allowlist:\n    - uv run pytest\n"
    web["response"] = web["client"].post(f"/repos/{name}/config", data={"config": config, "domain": ""}, follow_redirects=False)


@when(parsers.parse('I send back the spec gate of {ticket} with "{note}"'))
def send_back_with_note(web, ticket, note):
    web["note"] = note
    form = {"lane": "", "answer": "send_back", "note": note, "back": "/inbox"}
    assert web["client"].post(f"/missions/{ticket}/gates", data=form, follow_redirects=False).status_code == 303


@then(parsers.parse('the specifier\'s next step got a handoff from the human with "{note}"'))
def specifier_got_note(web, note):
    incoming = [h for role, h in web["service"].backend.requests if role == "specifier"][-1]
    assert incoming is not None and incoming.from_role == "human" and note in incoming.summary


@then(parsers.parse("the activity of {ticket} shows the instructions"))
def activity_note(web, ticket):
    assert f"spec gate: send_back · “{web['note']}”" in web["client"].get(f"/missions/{ticket}").text


@when("I reconnect to the live feed having seen every event")
def reconnect(web):
    last = web["service"].events.list()[-1].id
    web["feed"] = web["client"].get("/events/stream?since=0&once=true", headers={"Last-Event-ID": str(last)}).text


@then("it sends nothing")
def sends_nothing(web):
    assert "event:" not in web["feed"]


# --- managed environments ---------------------------------------------------------


@when(parsers.parse('I save {name}\'s variable {key} as "{value}"'))
@given(parsers.parse('I save {name}\'s variable {key} as "{value}"'))
def save_var(web, name, key, value):
    web["response"] = web["client"].post(f"/repos/{name}/env", data={"op": "set", "key": key, "value": value}, follow_redirects=False)
    assert web["response"].status_code == 303 and "env_error" not in web["response"].headers["location"]


@when(parsers.parse('I paste a .env into {name} with {key} "{value}"'))
def paste_env(web, name, key, value):
    response = web["client"].post(f"/repos/{name}/env", data={"op": "import", "text": f"# local\n{key}={value}\n"}, follow_redirects=False)
    assert "Imported+1+variable" in response.headers["location"]


@then(parsers.parse("{name}'s environment lists {first} and {second} masked"))
def env_listed(web, name, first, second):
    html = web["client"].get(f"/repos/{name}").text
    assert '<h2 id="env">' not in html[: html.index("</title>")]  # in the page, not the tab title
    page = html[html.index("<main") :]
    for key in (first, second):
        assert re.search(rf'data-var="{key}"><td class="mono">{key}</td><td class="mono">••••', page)


@then(parsers.parse('the value "{secret}" appears nowhere on the page or in the database'))
def secret_hidden(web, secret):
    assert secret not in web["client"].get(f"/repos/{web['repo']}").text
    dump = "\n".join(sqlite3.connect(web["service"].db).iterdump())
    assert secret not in dump
    path = web["service"].root / "env" / f"{web['repo']}.env"
    assert secret in path.read_text() and stat.S_IMODE(path.stat().st_mode) == 0o600


@given(parsers.parse("{name}'s local config marks it sensitive"))
def marks_sensitive(web, name):
    config = "stack: python\nsensitive: true\n"
    assert web["client"].post(f"/repos/{name}/config", data={"config": config, "domain": ""}, follow_redirects=False).status_code == 303


@then(parsers.parse("{name}'s {key} is flagged as a live Stripe key"))
def flagged(web, name, key):
    row = re.search(rf'data-var="{key}">(.*?)</tr>', web["client"].get(f"/repos/{name}").text, re.S).group(1)
    assert "looks like a live Stripe key" in row and "sk_live_51H" not in row


@when(parsers.parse('I start mission {ticket} on {name} overriding {key} with "{value}"'))
def start_with_override(web, ticket, name, key, value):
    web["override"] = value
    form = {"ticket": ticket, "title": "Add from_cents", "repo_urls": str(web["origin"]), "pack": "codec-standard", f"env:{name}": f"{key}={value}\n"}
    response = web["client"].post("/missions", data=form, follow_redirects=False)
    assert response.status_code == 303, response.text[:300]


@then(parsers.parse('{name}\'s lane in {ticket} gets {key} "{value}"'))
def lane_gets(web, name, ticket, key, value):
    assert web["service"].envs.for_lane(ticket, name)[key] == value


@then(parsers.parse("the mission's stored request names {key} but not its value"))
def stored_names_only(web, key):
    request, _ = web["service"].stored_request("CODEC-1770")
    assert request.env_overrides == {web["repo"]: [key]}
    dump = "\n".join(sqlite3.connect(web["service"].db).iterdump())
    assert web["override"] not in dump


# --- restart and update from base -----------------------------------------------------


@when(parsers.parse("I restart {ticket} from its page"))
def restart_page(web, ticket):
    web["old_backend"] = getattr(web["service"], "backend", None)
    web["response"] = web["client"].post(f"/missions/{ticket}/restart", follow_redirects=False)


@then(parsers.parse("{ticket} is on its second run, waiting at the spec gate"))
def second_run(web, ticket):
    view = web["service"].events.list(ticket)
    from codec_swarm.store.views import mission_view

    m = mission_view(view)
    assert m.runs == 2 and m.planning_gate is not None and m.planning_gate.kind == "spec"
    assert not any(lane.gate for lane in m.lanes.values())


@then("the specifier ran again")
def specifier_again(web):
    backend = web["service"].backend
    assert backend is not web["old_backend"] and backend.calls[0] == "specifier"


@when(parsers.parse("I open the restart form of {ticket}"))
def restart_form(web, ticket):
    web["page"] = web["client"].get(f"/missions/{ticket}/restart").text


@then(parsers.parse('it is filled with the title "{title}"'))
def filled_title(web, title):
    assert f'value="{title}"' in web["page"] and 'name="restart" value="1"' in web["page"]


@when(parsers.parse('I restart {ticket} with the title "{title}"'))
def restart_with_title(web, ticket, title):
    form = {"ticket": ticket, "title": title, "repo_urls": "codec-swarm-sandbox", "pack": "codec-standard", "restart": "1"}
    assert web["client"].post("/missions", data=form, follow_redirects=False).status_code == 303


@then(parsers.parse('mission {ticket} now has the title "{title}"'))
def now_titled(web, ticket, title):
    request, _ = web["service"].stored_request(ticket)
    assert request.title == title, [(e.kind, e.payload.get("error")) for e in web["service"].events.list(ticket)][-6:]


@given(parsers.parse("{ticket} is in the middle of a step"))
def busy(web, ticket, monkeypatch):
    monkeypatch.setattr(web["service"], "is_busy", lambda t: t == ticket)


@then(parsers.parse('the mission page says "{text}"'))
def mission_says(web, text):
    location = web["response"].headers["location"]
    assert web["response"].status_code == 303 and "error=" in location
    assert text in web["client"].get(location).text


@given(parsers.parse('{repo}\'s checks are "{command}"'))
def repo_checks(web, repo, command):
    folder = web["service"].root / "repos.d"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{repo}.yaml").write_text(f"stack: python\nbranch_flow:\n  base: develop\nchecks:\n  - id: unit\n    run: {command}\n")


def _push_to_develop(web, path, text):
    work = web["tmp"] / "upstream"
    if not work.exists():
        _git(web["tmp"], "clone", "-q", "-b", "develop", str(web["tmp"] / "origin.git"), "upstream")
    target = work / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    _git(work, "add", ".")
    _git(work, "-c", "user.name=t", "-c", "user.email=t@l", "commit", "-qm", f"add {path}")
    _git(work, "push", "-q", "origin", "develop")


@given("origin's develop gets a new commit adding docs/CHANGELOG.md")
def upstream_commit(web):
    _push_to_develop(web, "docs/CHANGELOG.md", "# changes\n")


@given("origin's develop gets a conflicting src/refunds.ts")
def upstream_conflict(web):
    _push_to_develop(web, "src/refunds.ts", "export const refund = () => 'theirs';\n")


@when(parsers.parse("I update {repo} of {ticket} from its base"))
def update_lane(web, repo, ticket):
    web["pr_before"] = web["service"].local_pr(ticket, repo)[0]
    response = web["client"].post(f"/missions/{ticket}/lanes/{repo}/update", data={}, follow_redirects=False)
    assert response.status_code == 303 and "error=" not in response.headers["location"], response.headers["location"]


@then(parsers.parse("the local PR of {ticket} for {repo} sits on the new develop with one commit"))
def rebased_pr(web, ticket, repo):
    import subprocess

    pr = web["service"].local_pr(ticket, repo)[0]
    develop = subprocess.run(["git", "rev-parse", "origin/develop"], cwd=pr.path, capture_output=True, text=True).stdout.strip()
    assert pr.base_sha == develop and pr.base_sha != web["pr_before"].base_sha
    assert subprocess.run(["git", "rev-list", "--count", f"{develop}..HEAD"], cwd=pr.path, capture_output=True, text=True).stdout.strip() == "1"
    assert (pr.path / "docs" / "CHANGELOG.md").exists()


@then(parsers.parse("the activity of {ticket} says the checks pass"))
def checks_pass(web, ticket):
    assert "checks pass" in web["client"].get(f"/missions/{ticket}").text


@then(parsers.parse("the activity of {ticket} says it conflicts in {path}"))
def says_conflict(web, ticket, path):
    assert f"conflicts with codec-payment in {path}; nothing was changed" in web["client"].get(f"/missions/{ticket}").text


@then(parsers.parse("the local PR of {ticket} for {repo} is unchanged"))
def pr_unchanged(web, ticket, repo):
    import subprocess

    pr = web["service"].local_pr(ticket, repo)[0]
    assert pr.sha == web["pr_before"].sha
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=pr.path, capture_output=True, text=True).stdout.strip() == pr.sha
    assert subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=pr.path, capture_output=True, text=True).stdout == ""


@when(parsers.parse('I restart lane {repo} of {ticket} with "{note}"'))
def restart_lane(web, repo, ticket, note):
    response = web["client"].post(f"/missions/{ticket}/lanes/{repo}/restart", data={"note": note}, follow_redirects=False)
    assert response.status_code == 303 and "error=" not in response.headers["location"]


@then(parsers.parse('the backend-coder\'s first step got "{note}"'))
def first_step_note(web, note):
    backend = web["service"].backend
    assert backend.calls[0] == "backend-coder" and backend.messages[0] == ("backend-coder", (note,))


@then("the specifier did not run again")
def no_specifier(web):
    assert "specifier" not in web["service"].backend.calls


# --- worker process -----------------------------------------------------------------


@given("the dashboard sends missions to a worker queue")
def queued_dashboard(web):
    from codec_swarm.store.commands import CommandQueue

    web["service"] = FakeMissionService(web["tmp"] / "root")
    web["queue"] = CommandQueue(web["service"].db)
    web["app"] = create_app(web["service"], "s3cret", web["queue"])
    web["client"] = TestClient(web["app"])
    web["token"] = "s3cret"


@then(parsers.parse("the start of {ticket} is queued and nothing has run yet"))
def queued(web, ticket):
    assert web["queue"].is_busy(ticket) and web["queue"].pending_count() == 1
    assert web["service"].events.list(ticket) == []


@then(parsers.parse("the mission page of {ticket} shows it as working"))
def page_working(web, ticket):
    # Nothing is in the event log yet, so the page is a 404 until the worker starts; the board shows the queue.
    assert "1 queued" in web["client"].get("/").text


@when("the worker runs what is queued")
def worker_runs(web):
    from codec_swarm.worker import Worker

    async def main():
        stop = anyio.Event()
        worker = Worker(web["service"], web["queue"])
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(worker.run, stop)
            with anyio.fail_after(10):
                while web["queue"].pending_count() or any(web["queue"].is_busy(t) for t in ("CODEC-1800", "CODEC-1801")):
                    await anyio.sleep(0.05)
            stop.set()

    anyio.run(main)


@given(parsers.parse("a worker died while approving the spec of {ticket}"))
def died_mid_command(web, ticket):
    web["queue"].submit(ticket, "answer", {"lane": "", "answer": "approve"})
    web["queue"].claim("dead-worker")  # claimed, never finished


@then(parsers.parse("{ticket} was resumed from its checkpoint, with my approval applied"))
def resumed(web, ticket):
    from codec_swarm.store.views import mission_view

    events = web["service"].events.list(ticket)
    assert "mission.interrupted" in [e.kind for e in events]
    view = mission_view(events)
    assert view.planning_gate is None and view.lanes["codec-swarm-sandbox"].gate.kind == "pr"  # the approval went through



# --- questions while planning -------------------------------------------------------


@given(parsers.parse('the specifier will ask "{first}" and "{second}"'))
def specifier_asks(web, first, second):
    web["service"].backend_options = {"questions_on": {"specifier": [first, second]}}
    web["questions"] = [first, second]


@then(parsers.parse("the inbox lists both questions of {ticket} with an answer box each"))
def inbox_questions(web, ticket):
    html = web["client"].get("/inbox").text
    gate = html[html.index(f'data-gate="{ticket}::questions"') :]
    for i, question in enumerate(web["questions"]):
        assert question in gate and f'name="a{i}"' in gate
    assert "Send answers to the specifier" in gate and "Continue without answering" in gate


@when(parsers.parse('I answer the questions of {ticket} with "{first}" and nothing, and send them'))
def answer_questions(web, ticket, first):
    form = {"lane": "", "answer": "send_back", "kind": "questions", "after": "specifier", "back": "/inbox",
            "q0": web["questions"][0], "r0": "specifier", "a0": first, "q1": web["questions"][1], "r1": "specifier", "a1": ""}
    assert web["client"].post(f"/missions/{ticket}/gates", data=form, follow_redirects=False).status_code == 303


@then(parsers.parse('the specifier\'s next step got the answer "{answer}" and was told to decide the other itself'))
def specifier_got_answers(web, answer):
    incoming = [h for role, h in web["service"].backend.requests if role == "specifier"][-1]
    assert incoming.from_role == "human"
    assert web["questions"][0] in incoming.summary and f"Answer: {answer}" in incoming.summary
    assert "no answer: decide yourself and state the assumption" in incoming.summary


@given(parsers.parse('mission {ticket} "{title}" on {repo} has the criteria "{first}" and "{second}" and a spec covering only the first'))
def mission_with_criteria(web, ticket, title, repo, first, second):
    description = f"Acceptance criteria\n- {first}\n- {second}\n"
    request = MissionRequest(ticket=ticket, title=title, repo_urls=(repo,), description=description, no_jev=True)
    anyio.run(web["service"].start, request)
    spec = web["service"].root / "missions" / ticket / repo / "spec"
    spec.mkdir(parents=True)
    (spec / "fixes.feature").write_text(
        "Feature: Invoice templates\n\n  @AC-1\n  Scenario: Merge duplicate templates\n    Then one template remains\n"
    )


@then("the Definition of Done shows AC-1 covered by a scenario and AC-2 covered by none")
def dod_criteria(web):
    dod = web["page"][web["page"].index('data-tab="dod"') :]
    ac1 = re.search(r'data-ac="1">(.*?)</div>', dod, re.S).group(1)
    ac2 = re.search(r'data-ac="2">(.*?)</div>', dod, re.S).group(1)
    assert "1 scenario" in ac1 and "no scenario covers it yet" in ac2


@then(parsers.parse('it lists the scenario "{name}" as not judged'))
def dod_scenario(web, name):
    dod = web["page"][web["page"].index('data-tab="dod"') :]
    assert name in dod and "not judged" in dod and "AC-1</span>" in dod
