"""Step definitions for features/m3_packs_metrics.feature. Engine steps come from conftest.py."""

import anyio
from pytest_bdd import given, parsers, scenarios, then, when

from codec_swarm.domain import Mission, choose_pack_rule
from codec_swarm.harness import load_pack
from codec_swarm.plugins.fake import FakeBackend, FakeJudge
from codec_swarm.plugins.jev.packs import choose_pack_jev
from codec_swarm.store.metrics import MissionMetrics, compare, mission_metrics

scenarios("../features/m3_packs_metrics.feature")


@given("the Solo pack and a fake backend")
def solo_pack(world):
    world["pack"] = load_pack("solo").pack
    world["backend"] = FakeBackend()
    world["judge"] = FakeJudge()


@given(parsers.parse("a ticket {with_description} acceptance criteria on {repos:d} repo(s), sensitive: {sensitive}"))
def ticket(world, with_description, repos, sensitive):
    description = "It must raise ValueError for negative amounts." if with_description == "with" else ""
    world["ticket"] = Mission(ticket="CODEC-1500", repo="codec-swarm-sandbox", title="Reject negative amounts", description=description)
    world["repos"], world["sensitive"] = repos, sensitive == "yes"


@given(parsers.parse("Jev would pick {pack} with probability {p:f}"))
def jev_pick(world, pack, p):
    async def ask(state, questions):
        from typesafe_sdk import SystemOneResponse

        others = [o for o in questions["pack"].criteria if o != pack]
        probabilities = {pack: p, **{o: (1 - p) / len(others) for o in others}}
        answer = {"type": "choice", "choice": pack, "confidence": p, "probabilities": probabilities}
        return SystemOneResponse.model_validate({"model": "jev-1.13.0", "usage": {}, "answers": {"pack": answer}})

    world["ask"] = ask


@when("the pack is chosen without Jev")
def choose_without_jev(world):
    world["choice"] = choose_pack_rule(bool(world["ticket"].description), world["repos"], world["sensitive"])


@when("the pack is chosen with Jev")
def choose_with_jev(world):
    world["choice"] = anyio.run(choose_pack_jev, world["ask"], world["ticket"], world["repos"], world["sensitive"])


@then(parsers.parse("the pack is {pack}, decided by {source}"))
def pack_is(world, pack, source):
    assert (world["choice"].next, world["choice"].source) == (pack, source), world["choice"]


@when(parsers.parse("the metrics are computed for {ticket}"))
def computed(world, ticket):
    world["metrics"] = mission_metrics(world["events"].list(ticket))


@then(parsers.parse("the metrics show {steps:d} agent steps, {sendbacks:d} send-back, {judges:d} judge run and {gates:d} gates"))
def metric_counts(world, steps, sendbacks, judges, gates):
    m = world["metrics"]
    assert (m.agent_steps, m.sendbacks, m.judge_runs, m.gates) == (steps, sendbacks, judges, gates), m


@then(parsers.parse("the metrics show the mission ended {status}"))
def metric_status(world, status):
    assert world["metrics"].status == status


@given("recorded metrics for 2 Solo missions without Jev and 2 Codec standard missions with Jev")
def recorded(world):
    world["recorded"] = [
        MissionMetrics(ticket="A", pack="solo", jev=False, status="pr_ready", minutes_to_pr=6, agent_cost_usd=0.40, sendbacks=0),
        MissionMetrics(ticket="B", pack="solo", jev=False, status="pr_ready", minutes_to_pr=8, agent_cost_usd=0.60, sendbacks=2),
        MissionMetrics(ticket="C", pack="codec-standard", jev=True, status="pr_ready", minutes_to_pr=7, agent_cost_usd=1.00, sendbacks=0),
        MissionMetrics(ticket="D", pack="codec-standard", jev=True, status="waiting", agent_cost_usd=1.40, sendbacks=1),
    ]


@when("the report groups them")
def groups(world):
    world["rows"] = compare(world["recorded"])


@then("it shows one row per pack and Jev setting with mean cost, mean time to PR and send-backs per mission")
def report_rows(world):
    rows = {(r.pack, r.jev): r for r in world["rows"]}
    assert set(rows) == {("solo", False), ("codec-standard", True)}
    solo, standard = rows[("solo", False)], rows[("codec-standard", True)]
    assert (solo.mean_cost_usd, solo.mean_minutes_to_pr, solo.sendbacks_per_mission) == (0.5, 7.0, 1.0)
    assert (standard.missions, standard.pr_ready, standard.mean_minutes_to_pr) == (2, 1, 7.0)
