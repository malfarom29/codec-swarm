"""Step definitions for features/m1_engine.feature. Fake backend and judge: no tokens spent."""

from collections import Counter

import anyio
import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from codec_swarm.domain import CODEC_STANDARD, Autonomy, Mission
from codec_swarm.graph import APPROVE, MissionRunner
from codec_swarm.plugins.fake import BackendCrash, FakeBackend, FakeJudge
from codec_swarm.plugins.fallback import PackOrderRouter
from codec_swarm.store import EventLog

scenarios("../features/m1_engine.feature")

MAX_GATES = 20  # guards the "approve every gate" loops against a graph that never ends


@pytest.fixture
def world(tmp_path):
    w = {"db": tmp_path / "swarm.db"}
    w["events"] = EventLog(w["db"])
    yield w
    w["events"].close()


def _runner(world, backend):
    return MissionRunner(world["db"], world["pack"], backend, PackOrderRouter(), world["judge"], world["events"])


def _approve_all(world):
    for _ in range(MAX_GATES):
        if world["result"].status != "waiting":
            return
        try:
            world["result"] = anyio.run(world["runner"].answer, world["mission"].ticket, APPROVE)
        except BackendCrash as error:
            world["error"] = error
            return
    raise AssertionError("still waiting after approving 20 gates")


@given("the Codec standard pack")
def pack(world):
    world["pack"] = CODEC_STANDARD


@given("a fake backend")
def fake_backend(world):
    world["backend"] = FakeBackend()
    world["judge"] = FakeJudge()


@given(parsers.re(r"an? (?P<autonomy>\w+) mission for (?P<ticket>\S+) on (?P<repo>\S+)"))
def mission(world, autonomy, ticket, repo):
    world["mission"] = Mission(ticket=ticket, repo=repo, autonomy=Autonomy(autonomy))


@given(parsers.parse("the {role} sends back its first handoff"))
def sends_back(world, role):
    world["backend"].send_back_once.add(role)


@given(parsers.parse("the judge scores {scores}"))
def judge_scores(world, scores):
    world["judge"].scores = [float(s) for s in scores.split(" then ")]


@given(parsers.parse("the backend crashes when the {role} starts"))
def crashes(world, role):
    world["backend"].crash_on = role


@when("the mission runs")
def runs(world):
    world["runner"] = _runner(world, world["backend"])
    try:
        world["result"] = anyio.run(world["runner"].start, world["mission"])
    except BackendCrash as error:
        world["error"] = error


@when("the mission runs and I approve every gate")
def runs_and_approves(world):
    runs(world)
    if "error" not in world:
        approve_every_gate(world)


@when("I approve the gate")
def approve_gate(world):
    world["result"] = anyio.run(world["runner"].answer, world["mission"].ticket, APPROVE)


@when("I approve every gate")
def approve_every_gate(world):
    _approve_all(world)


@when("a new runner recovers the mission from the same database")
def recovers(world):
    world["backend_after_crash"] = FakeBackend()
    world["runner"] = _runner(world, world["backend_after_crash"])
    world["result"] = anyio.run(world["runner"].recover, world["mission"].ticket)


@then(parsers.parse("it waits at the {kind} gate"))
def waits_at(world, kind):
    assert world["result"].status == "waiting"
    assert world["result"].gate["kind"] == kind


@then(parsers.parse("it waits at the handoff gate after the {role}"))
def waits_after(world, role):
    waits_at(world, "handoff")
    assert world["result"].gate["after"] == role


@then("the mission is PR ready")
def pr_ready(world):
    assert world["result"].status == "pr_ready", world["result"]


@then(parsers.parse("the roles ran in order: {roles}"))
def roles_in_order(world, roles):
    assert world["result"].trail == [r.strip() for r in roles.split(",")]


@then(parsers.parse("the judge ran {count:d} times"))
def judge_runs(world, count):
    assert world["judge"].calls == count


@then(parsers.parse("the run fails while the {role} works"))
def run_fails(world, role):
    assert isinstance(world.get("error"), BackendCrash)
    assert role in str(world["error"])


@then("no role before the hardener ran twice")
def no_rerun_before_crash(world):
    calls = Counter(world["backend"].calls + world["backend_after_crash"].calls)
    for role in ("specifier", "architect", "backend-coder", "reviewer"):
        assert calls[role] == 1, calls


@then(parsers.parse("the event log has {handoffs:d} handoffs, {opened:d} opened gates, {resolved:d} resolved gates and {verdicts:d} verdict"))
def event_counts(world, handoffs, opened, resolved, verdicts):
    kinds = Counter(e.kind for e in world["events"].list(world["mission"].ticket))
    assert (kinds["handoff"], kinds["gate.opened"], kinds["gate.resolved"], kinds["verdict"]) == (handoffs, opened, resolved, verdicts)


@then("the event ids increase monotonically")
def monotonic_ids(world):
    ids = [e.id for e in world["events"].list(world["mission"].ticket)]
    assert ids == sorted(ids) and len(ids) == len(set(ids))
