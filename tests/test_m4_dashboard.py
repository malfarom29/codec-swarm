"""Step definitions for features/m4_dashboard.feature: the web app over a fake mission service."""

import re

import anyio
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
from codec_swarm.web.app import create_app

scenarios("../features/m4_dashboard.feature")


class FakeMissionService(MissionService):
    """The real service's start/answer/recover, with missions built from the fake backend and judge."""

    async def build(self, request: MissionRequest, pack_name: str | None = None) -> MissionRuntime:
        names = tuple(repo_name(u) for u in request.repo_urls)
        mission = Mission(ticket=request.ticket, repo="", repos=names, title=request.title, description=request.description, autonomy=request.autonomy)
        backend, judge = FakeBackend(), FakeJudge()

        def runner(part):
            return MissionRunner(self.db, CODEC_STANDARD, backend, PackOrderRouter(), judge, self.events, part=part)

        coordinator = MissionCoordinator(runner("planning"), runner("lane"), self.events)
        runtime = MissionRuntime(request, mission, "codec-standard", False, coordinator, JevClient())
        self._runtimes[request.ticket] = runtime
        return runtime


@pytest.fixture
def web(tmp_path):
    return {"tmp": tmp_path}


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
    row = re.search(r'data-role="specifier">(.*?)</tr>', web["response"].text, re.S).group(1)
    assert ">opus<" in row and ">sonnet<" in row


@then("the page lists the backend-coder's MCP server context7")
def harness_mcp(web):
    row = re.search(r'data-role="backend-coder">(.*?)</tr>', web["response"].text, re.S).group(1)
    assert "context7" in row


@then("the page shows the judge bands 0.95 and 0.80")
def orchestration_bands(web):
    assert "approve ≥ 0.95 · review ≥ 0.80" in web["response"].text


@then("the page shows the command-gate band from 0.87 to 0.93")
def orchestration_gate(web):
    assert "unsure 0.87–0.93" in web["response"].text
