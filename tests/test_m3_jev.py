"""Step definitions for features/m3_jev.feature, against a scripted fake Jev: no API calls."""

import json

import anyio
import pytest
from pytest_bdd import given, parsers, scenarios, then, when
from typesafe_sdk import SystemOneResponse, TypeSafeError

from codec_swarm.domain import Autonomy, Handoff, JudgeBands, Mission
from codec_swarm.harness import Check, load_pack, load_repo_config
from codec_swarm.plugins.checks import ChecksOnlyJudge
from codec_swarm.plugins.fallback import PackOrderRouter
from codec_swarm.plugins.gate import AllowlistGate, GateContext
from codec_swarm.plugins.jev import JevClient, JevCommandGate, JevJudge, JevRouter, LaneUnderJudgement
from spikes import m0_jev
from codec_swarm.plugins.registry import build_plugins
from codec_swarm.store import GateCache

scenarios("../features/m3_jev.feature")

MISSION = Mission(ticket="CODEC-1423", repo="codec-payment", title="Partial refunds", autonomy=Autonomy.AUTO)
COUNTS = {"never": 0, "once": 1, "twice": 2}


class FakeJev:
    """Answers each question kind from a script and counts calls; `down` simulates an outage."""

    def __init__(self):
        self.calls = 0
        self.down = False
        self.model = "sonnet"
        self.next_role = ("reviewer", 0.9)
        self.safe = 0.5
        self.done = 0.5
        self.scenarios: dict[str, float] = {}  # scenario name -> probability; others get 0.9
        self.criteria: dict[str, float] = {}  # ticket criterion text -> probability; others get 0.9
        self.states: list = []

    async def __call__(self, state, questions):
        self.calls += 1
        self.states.append(state)
        if self.down:
            raise TypeSafeError("Jev is unavailable")
        answers = {}
        for name, question in questions.items():
            if name == "model":
                answers[name] = self._choice(self.model, 0.8, list(question.criteria))
            elif name == "next_role":
                target, p = self.next_role
                answers[name] = self._choice(target, p, list(question.criteria))
            elif name == "safe":
                answers[name] = {"type": "noul", "noul": self.safe}
            elif name == "done":
                answers[name] = {"type": "noul", "noul": self.done}
            elif "criterion" in question.instructions:  # criterion_i: a ticket criterion no scenario covers
                answers[name] = {"type": "noul", "noul": self.criteria.get(question.instructions["criterion"], 0.9)}
            else:  # scenario_i
                text = question.instructions["scenario"]
                p = next((v for k, v in self.scenarios.items() if f": {k}\n" in text + "\n"), 0.9)
                answers[name] = {"type": "noul", "noul": p}
        return SystemOneResponse.model_validate({"model": "jev-1.13.0", "usage": {"input_tokens": 1, "output_tokens": 1}, "answers": answers})

    @staticmethod
    def _choice(choice, p, options):
        rest = [o for o in options if o != choice]
        probabilities = {choice: p, **{o: round((1 - p) / len(rest), 4) for o in rest}} if rest else {choice: 1.0}
        return {"type": "choice", "choice": choice, "confidence": p, "probabilities": probabilities}


@pytest.fixture
def world(tmp_path):
    return {"tmp": tmp_path, "jev": FakeJev(), "pack": load_pack("codec-standard")}


# --- registry ----------------------------------------------------------------


@given("TYPESAFE_API_KEY is not set")
def no_key(world):
    world["env"] = {}


@when("the registry picks the plugins for a mission that asks for Jev")
def registry_picks(world):
    config = load_repo_config(world["tmp"], world["pack"], stack="nestjs")
    lane = LaneUnderJudgement(worktree=world["tmp"], base="develop", checks=())
    world["plugins"] = build_plugins(world["pack"], config, lambda m: lane, GateCache(world["tmp"] / "db"), use_jev=True, env=world["env"])


@then("the router, command gate and judge are the fixed-rule fallbacks")
def fallbacks(world):
    p = world["plugins"]
    assert not p.jev
    assert isinstance(p.router, PackOrderRouter) and isinstance(p.gate, AllowlistGate) and isinstance(p.judge, ChecksOnlyJudge)


# --- router ------------------------------------------------------------------


@given("Jev is on and would pick haiku for every role")
def picks_haiku(world):
    world["jev"].model = "haiku"


@given(parsers.parse("Jev is on and would send the hardener's work to the backend-coder with probability {p:f}"))
def sends_to_coder(world, p):
    world["jev"].next_role = ("backend-coder", p)


@given("Jev is on but unavailable")
def jev_down(world):
    world["jev"].down = True


def _router(world):
    return JevRouter(world["pack"], world["jev"])


@when(parsers.parse("the router picks the model for the {role}"))
def picks_model(world, role):
    world["decision"] = anyio.run(_router(world).pick_model, world["pack"].pack, MISSION, role, None)


@then(parsers.parse("the model is {model}, the specifier's floor"))
def model_is(world, model):
    assert world["decision"].next == model


@then(parsers.parse("the decision source is {source}"))
def decision_source(world, source):
    assert world["decision"].source == source


def _route(world, role, send_back):
    handoff = Handoff(from_role=role, summary="found a defect" if send_back else "done", send_back=send_back)
    world["decision"] = anyio.run(_router(world).next_role, world["pack"].pack, role, handoff)


@when(parsers.parse("the {role} sends its work back"))
def sends_back(world, role):
    _route(world, role, True)


@when(parsers.parse("the {role} hands off without sending back"))
def hands_off(world, role):
    _route(world, role, False)


@then(parsers.parse("the next role is {role}, decided by {source}"))
def next_role_is(world, role, source):
    assert (world["decision"].next, world["decision"].source) == (role, source)


@then(parsers.parse("Jev was {count} asked"))
def jev_asked_never(world, count):
    assert world["jev"].calls == COUNTS[count]


@then(parsers.parse("Jev was asked {count}"))
def jev_asked(world, count):
    assert world["jev"].calls == COUNTS[count]


# --- command gate ------------------------------------------------------------


@given(parsers.parse("the Jev command gate with threshold {threshold:f} and margin {margin:f}"))
def jev_gate(world, threshold, margin):
    world["gate"] = JevCommandGate(world["jev"], ("npm run test",), GateCache(world["tmp"] / "swarm.db"), threshold, margin)


@given(parsers.parse("Jev scores every command {score:f} safe"))
def scores(world, score):
    world["jev"].safe = score


@given(parsers.parse('the repo\'s package.json has the script {name} "{script}"'))
@when(parsers.parse('the repo\'s package.json script {name} becomes "{script}"'))
def package_script(world, name, script):
    (world["tmp"] / "package.json").write_text(json.dumps({"scripts": {name: script}}))


def _ask_gate(world, command, autonomy):
    ctx = GateContext(ticket="CODEC-1423", repo="codec-payment", role="backend-coder", worktree=world["tmp"], autonomy=Autonomy(autonomy))
    return anyio.run(world["gate"].decide, "Bash", {"command": command}, ctx)


@when(parsers.parse('an agent asks to run "{command}" in {autonomy} mode'))
def asks(world, command, autonomy):
    world["gate_decision"] = _ask_gate(world, command, autonomy)


@when(parsers.parse('an agent asks to run "{command}" in auto mode twice'))
def asks_twice(world, command):
    _ask_gate(world, command, "auto")
    world["gate_decision"] = _ask_gate(world, command, "auto")


@then(parsers.parse("the gate answers {answer} with source {source}"))
def gate_answer(world, answer, source):
    d = world["gate_decision"]
    assert (d.action.value, d.source) == (answer, source), d


@then(parsers.parse('running "{command}" in auto mode asks a human'))
def asks_human(world, command):
    assert _ask_gate(world, command, "auto").action.value == "ask"


# --- judge -------------------------------------------------------------------


@given(parsers.parse("the Jev judge with the repo's checks {state}"))
def jev_judge(world, state):
    code = "0" if state == "passing" else "1"
    checks = (Check(id="unit", run=f"python3 -c 'raise SystemExit({code})'"),)
    lane = LaneUnderJudgement(worktree=world["tmp"], base="develop", checks=checks)
    world["judge"] = JevJudge(world["jev"], lambda m: lane)


@given(parsers.parse('Jev scores scenario "{name}" at {p:f}'))
def scenario_score(world, name, p):
    world["jev"].scenarios[name] = p


@given(parsers.parse("a Jev judge whose passing check writes a JUnit report of {total:d} tests, {failing:d} failing"))
def judge_with_junit(world, total, failing):
    from codec_swarm.harness.config import Report

    cases = "".join(
        f'<testcase classname="tests.test_cents" name="test_{i}">' + ("<failure message='boom'/>" if i < failing else "") + "</testcase>"
        for i in range(total)
    )
    (world["tmp"] / "reports").mkdir()
    (world["tmp"] / "reports" / "junit.xml").write_text(f'<testsuites><testsuite name="pytest">{cases}</testsuite></testsuites>')
    checks = (Check(id="unit", run="python3 -c 'raise SystemExit(0)'", report=Report(kind="junit", path="reports/junit.xml")),)
    lane = LaneUnderJudgement(worktree=world["tmp"], base="develop", checks=checks)
    world["judge"] = JevJudge(world["jev"], lambda m: lane)


@given("a lane whose base branch already has an earlier mission's spec file")
def lane_with_old_spec(world):
    import subprocess

    def git(cwd, *args):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    seed = world["tmp"] / "seed"
    (seed / ".swarm" / "spec").mkdir(parents=True)
    (seed / ".swarm" / "spec" / "old.feature").write_text("Feature: Old\n\n  Scenario: From an earlier mission\n    Then it was merged\n")
    git(seed, "init", "-q", "-b", "develop")
    git(seed, "add", ".")
    git(seed, "-c", "user.name=t", "-c", "user.email=t@l", "commit", "-qm", "old mission")
    git(world["tmp"], "clone", "-q", str(seed), "lane")
    world["lane_path"] = world["tmp"] / "lane"
    (world["lane_path"] / ".git" / "info" / "exclude").write_text("/.swarm/\n")  # as Workspace.clone sets up


@given(parsers.parse("the lane adds its own spec file with {count:d} scenarios"))
def lane_adds_spec(world, count):
    body = "\n".join(f"  Scenario: Split case {i}\n    Then it splits\n" for i in range(count))
    (world["lane_path"] / ".swarm" / "spec" / "split.feature").write_text(f"Feature: Split\n\n{body}")


@given("the Jev judge for that lane with passing checks")
def judge_for_lane(world):
    checks = (Check(id="unit", run="python3 -c 'raise SystemExit(0)'"),)
    lane = LaneUnderJudgement(worktree=world["lane_path"], base="develop", checks=checks)
    world["judge"] = JevJudge(world["jev"], lambda m: lane)


@then(parsers.parse("Jev was shown {count:d} test results"))
def shown_tests(world, count):
    assert len(world["jev"].states[-1]["tests"]) == count


@then(parsers.parse('the verdict rationale names "{name}"'))
def rationale_names(world, name):
    assert name in world["verdict"].rationale


@given(parsers.parse("Jev says the lane is done with probability {p:f}"))
def done_probability(world, p):
    world["jev"].done = p


@given(parsers.parse("the lane has {count:d} approved scenarios"))
def approved_scenarios(world, count):
    spec = world["tmp"] / ".swarm" / "spec"
    spec.mkdir(parents=True)
    body = "\n".join(f"  Scenario: Refund case {i}\n    Given a charge\n    Then it is refunded\n" for i in range(count))
    (spec / "refunds.feature").write_text(f"Feature: Refunds\n\n{body}")


@when("the judge evaluates the lane")
def evaluates(world):
    world["verdict"] = anyio.run(world["judge"].evaluate, MISSION, [])


@then(parsers.parse("the verdict score is {score:f} and the band is {band}"))
def verdict_band(world, score, band):
    assert world["verdict"].score == pytest.approx(score)
    assert JudgeBands().classify(world["verdict"]).value == band


@then(parsers.parse("the verdict lists a probability for each of the {count:d} scenarios"))
def scenario_probabilities(world, count):
    results = world["verdict"].scenarios
    assert len(results) == count and all(r.probability is not None for r in results)


# --- live smoke test ---------------------------------------------------------


@given("TYPESAFE_API_KEY is set")
def key_set():
    if not m0_jev.has_key():
        pytest.skip("TYPESAFE_API_KEY is not set")


@when(parsers.parse('the real Jev picks a model for the backend-coder, gates "{command}" and judges a lane'))
def real_jev(world, command):
    client = JevClient(timeout=30)
    checks = (Check(id="unit", run="python3 -c 'raise SystemExit(0)'"),)
    lane = LaneUnderJudgement(worktree=world["tmp"], base="develop", checks=checks)
    gate = JevCommandGate(client, (), GateCache(world["tmp"] / "swarm.db"))
    ctx = GateContext(ticket=MISSION.ticket, repo=MISSION.repo, role="backend-coder", worktree=world["tmp"], autonomy=Autonomy.AUTO)

    async def run():
        model = await JevRouter(world["pack"], client).pick_model(world["pack"].pack, MISSION, "backend-coder", None)
        decision = await gate.decide("Bash", {"command": command}, ctx)
        verdict = await JevJudge(client, lambda m: lane).evaluate(MISSION, [])
        await client.aclose()
        return model, decision, verdict

    world["model"], world["gate_decision"], world["verdict"] = anyio.run(run)


@then("the model is haiku, sonnet or opus")
def real_model(world):
    assert world["model"].next in ("haiku", "sonnet", "opus") and world["model"].source == "jev"


@then("the gate decision comes from jev")
def real_gate(world):
    assert world["gate_decision"].source.startswith("jev"), world["gate_decision"]


@then("the judge's score is a probability")
def real_score(world):
    assert 0.0 <= world["verdict"].score <= 1.0 and world["verdict"].source == "jev"
