"""A planning role that asks something waits for my answers before the spec is final."""

import anyio

from codec_swarm.domain import CODEC_STANDARD, Autonomy, Mission
from codec_swarm.graph import APPROVE, SEND_BACK, MissionRunner
from codec_swarm.plugins.fake import FakeBackend, FakeJudge
from codec_swarm.plugins.fallback import PackOrderRouter
from codec_swarm.store import EventLog


def _planning(tmp_path, autonomy=Autonomy.GATED):
    backend = FakeBackend(questions_on={"specifier": ["Which column?", "Keep the enum values?"]})
    runner = MissionRunner(tmp_path / "swarm.db", CODEC_STANDARD, backend, PackOrderRouter(), FakeJudge(), EventLog(tmp_path / "swarm.db"), part="planning")
    return backend, runner, Mission(ticket="T-1", repo="", repos=("api",), autonomy=autonomy)


def test_the_specifiers_questions_open_a_questions_gate_and_answers_go_back_to_it(tmp_path):
    backend, runner, mission = _planning(tmp_path)
    result = anyio.run(runner.start, mission)
    assert result.gate["kind"] == "questions" and result.gate["after"] == "specifier"
    assert [q["question"] for q in result.gate["questions"]] == ["Which column?", "Keep the enum values?"]
    result = anyio.run(runner.answer, "T-1", SEND_BACK, "1. Which column?\n   Answer: template_id")
    assert backend.calls == ["specifier", "specifier", "architect"]
    assert "template_id" in backend.requests[1][1].summary
    assert result.gate["kind"] == "spec"


def test_continuing_without_answers_goes_on_to_the_architect(tmp_path):
    backend, runner, mission = _planning(tmp_path)
    anyio.run(runner.start, mission)
    result = anyio.run(runner.answer, "T-1", APPROVE)
    assert backend.calls == ["specifier", "architect"] and result.gate["kind"] == "spec"


def test_auto_mode_never_stops_for_questions(tmp_path):
    backend, runner, mission = _planning(tmp_path, Autonomy.AUTO)
    result = anyio.run(runner.start, mission)
    assert result.gate is None or result.gate["kind"] != "questions"
