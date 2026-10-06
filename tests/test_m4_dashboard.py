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
        backend, judge = FakeBackend(), FakeJudge()
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
