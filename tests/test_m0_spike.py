"""Step definitions for features/m0_spike.feature. Live scenarios run with `-m live`."""

import anyio
import pytest
from pytest_bdd import given, scenarios, then, when

from spikes import m0_agent_sdk as sdk
from spikes import m0_jev as jev

scenarios("../features/m0_spike.feature")


@given("the installed claude-agent-sdk", target_fixture="fields")
def fields():
    return sdk.check_fields()


@then("ClaudeAgentOptions has every field the harness needs")
def has_fields(fields):
    assert fields["missing"] == []


@given("a throwaway git repo with a CLAUDE.md", target_fixture="repo")
def repo():
    return sdk.make_repo()


@when("a Haiku session runs one task in it with can_use_tool bridged", target_fixture="turn")
def turn(repo):
    return anyio.run(sdk.first_turn, repo)


@then("the task's file is written inside the repo")
def note_written(turn):
    assert turn["note_written"], turn["reply"]


@then("every permission request went through the bridge")
def bridged(turn):
    assert turn["permission_log"], "can_use_tool was never called"
    assert all(entry["allowed"] for entry in turn["permission_log"])


@then("the result reports session_id, turns, tokens and cost")
def reports_totals(turn):
    result = turn["result"]
    assert result.get("session_id")
    assert result.get("num_turns")
    assert result.get("usage")
    assert result.get("total_cost_usd") is not None


@then("the reply follows the repo's CLAUDE.md")
def follows_claude_md(turn):
    assert turn["follows_claude_md"], turn["reply"]


@when("a new process resumes that session by session_id", target_fixture="resumed")
def resumed(repo, turn):
    return sdk.resume_in_new_process(repo, turn["result"]["session_id"])


@then("the resumed agent still knows the code word")
def knows_code_word(resumed):
    assert resumed["knows_code_word"], resumed["reply"]


@given("TYPESAFE_API_KEY is set")
def key_set():
    if not jev.has_key():
        pytest.skip("TYPESAFE_API_KEY is not set")


@when(
    "system_one asks a choice, a noul and a score question with model jev-latest",
    target_fixture="jev_report",
)
def jev_report():
    return anyio.run(jev.ask)


@then("each answer has the expected type and valid probabilities")
def answers_valid(jev_report):
    assert jev_report["pinned_model_available"], jev_report["models"]
    assert all(jev_report["answers_valid"].values()), jev_report["answers"]


@then("latency and token usage are recorded")
def latency_recorded(jev_report):
    assert jev_report["latency_ms"] and jev_report["usage"]
