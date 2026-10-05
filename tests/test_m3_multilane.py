"""Step definitions for features/m3_multilane.feature: the coordinator over planning and lane threads."""

import anyio
from pytest_bdd import given, parsers, scenarios, then, when

from codec_swarm.domain import Autonomy, LaneOrder, Mission
from codec_swarm.graph import APPROVE, MissionRunner
from codec_swarm.graph.coordinator import MissionCoordinator
from codec_swarm.plugins.fallback import PackOrderRouter

scenarios("../features/m3_multilane.feature")


@given(parsers.re(r"an? (?P<autonomy>\w+) mission for (?P<ticket>\S+) on (?P<first>\S+) and (?P<second>\S+)"))
def two_repo_mission(world, autonomy, ticket, first, second):
    world["mission"] = Mission(ticket=ticket, repo="", repos=(first, second), autonomy=Autonomy(autonomy))


@given(parsers.parse("the architect orders {later} after {earlier} and {earlier2} after {later2}"))
def cyclic_order(world, later, earlier, earlier2, later2):
    world["backend"].lane_order = (LaneOrder(repo=later, after=(earlier,)), LaneOrder(repo=earlier2, after=(later2,)))


@given(parsers.parse("the architect orders {later} after {earlier}"))
def lane_order(world, later, earlier):
    world["backend"].lane_order = (LaneOrder(repo=later, after=(earlier,)),)


@given("the architect gives no lane order")
def no_order(world):
    world["backend"].lane_order = ()


@given(parsers.parse("the backend crashes when the {role} starts in {repo}"))
def crash_in_repo(world, role, repo):
    world["backend"].crash_on = role
    world["backend"].crash_in_repo = repo


def _coordinator(world):
    def runner(part):
        return MissionRunner(world["db"], world["pack"], world["backend"], PackOrderRouter(), world["judge"], world["events"], part=part)

    return MissionCoordinator(runner("planning"), runner("lane"), world["events"])


@when("the mission runs")
def runs(world):
    world["coordinator"] = _coordinator(world)
    world["result"] = anyio.run(world["coordinator"].start, world["mission"])


@when("I approve the spec gate")
def approve_spec(world):
    world["result"] = anyio.run(world["coordinator"].answer, world["mission"].ticket, None, APPROVE)


@when(parsers.parse("I approve the pr gate of lane {repo}"))
def approve_lane(world, repo):
    assert world["result"].lanes[repo].gate["kind"] == "pr"
    world["result"] = anyio.run(world["coordinator"].answer, world["mission"].ticket, repo, APPROVE)


@then("the mission waits at the spec gate")
def waits_spec(world):
    assert world["result"].waiting() == [(None, world["result"].planning.gate)]
    assert world["result"].planning.gate["kind"] == "spec"


@then(parsers.parse("lane {repo} waits at the {kind} gate"))
def lane_waits(world, repo, kind):
    lane = world["result"].lanes[repo]
    assert lane.status == "waiting" and lane.gate["kind"] == kind, lane


@then(parsers.parse("lane {repo} is PR ready"))
def lane_ready(world, repo):
    assert world["result"].lanes[repo].status == "pr_ready"


@then("every lane is PR ready")
def all_ready(world):
    assert world["result"].done, world["result"].lanes


@then(parsers.parse("lane {repo} failed"))
def lane_failed(world, repo):
    assert world["result"].lanes[repo].status == "failed"


@then(parsers.parse("lane {repo} has not started"))
def lane_not_started(world, repo):
    assert world["result"].lanes[repo].status == "not_started"


@then("the mission is blocked because the lane order has a cycle")
def blocked(world):
    assert world["result"].blocked and "cycle" in world["result"].blocked
    assert not world["result"].lanes


def _event_ids(world, kind, lane=None):
    return [e.id for e in world["events"].list(world["mission"].ticket) if e.kind == kind and (lane is None or e.payload.get("lane") == lane)]


@then(parsers.parse("lane {later} started after lane {earlier} reached the judge"))
def started_after(world, later, earlier):
    verdicts = [e.id for e in world["events"].list(world["mission"].ticket) if e.kind == "verdict"]
    assert _event_ids(world, "lane.started", later)[0] > min(verdicts)
    assert _event_ids(world, "lane.started", earlier)[0] < min(verdicts)


@then("both lanes started right after the spec gate")
def started_together(world):
    first_verdict = min(e.id for e in world["events"].list(world["mission"].ticket) if e.kind == "verdict")
    assert all(i < first_verdict for i in _event_ids(world, "lane.started"))
    assert len(_event_ids(world, "lane.started")) == 2
