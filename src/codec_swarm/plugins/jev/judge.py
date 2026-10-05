"""Jev Judge: hard checks first, then Jev's probability that each scenario and the whole lane are done."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel
from typesafe_sdk import Noul, TypeSafeError

from codec_swarm.domain import Handoff, Mission, ScenarioResult, Verdict
from codec_swarm.harness.config import Check
from codec_swarm.plugins.checks import ChecksOnlyJudge, junit_results, run_check
from codec_swarm.plugins.jev.client import SystemOne
from codec_swarm.workspace.lanes import lane_spec_files

MAX_DIFF_CHARS = 12_000  # Jev sees a truncated diff and diff stats, never whole files or secrets
DONE = Noul(
    instructions="Is this change done by its Definition of Done: every approved scenario met and every hard check passing?"
)


class LaneUnderJudgement(BaseModel, frozen=True):
    worktree: Path
    base: str
    checks: tuple[Check, ...]


def load_scenarios(worktree: Path, base: str | None = None) -> list[tuple[str, str]]:
    """(name, text) for every scenario in this lane's approved spec under .swarm/spec/.

    Spec files already on the base branch belong to earlier missions (a squash merge can carry them in),
    so only the ones this lane added or changed count.
    """
    scenarios: list[tuple[str, str]] = []
    mine = lane_spec_files(worktree, base)
    for feature in sorted((worktree / ".swarm" / "spec").glob("*.feature")):
        if mine is not None and feature.relative_to(worktree).as_posix() not in mine:
            continue
        blocks = re.split(r"(?m)^(?=[ \t]*Scenario(?: Outline)?:)", feature.read_text())
        for block in (b.strip() for b in blocks[1:]):
            if block:
                scenarios.append((block.splitlines()[0].split(":", 1)[1].strip(), block))
    return scenarios


def _diff(worktree: Path, base: str) -> tuple[str, str]:
    def git(*args: str) -> str:
        proc = subprocess.run(["git", *args], cwd=worktree, capture_output=True, text=True)
        return proc.stdout if proc.returncode == 0 else ""

    spec = f"origin/{base}...HEAD"
    return git("diff", "--stat", spec), git("diff", spec, "--", ".", ":(exclude).swarm")[:MAX_DIFF_CHARS]


class JevJudge:
    def __init__(self, ask: SystemOne, lane_for: Callable[[Mission], LaneUnderJudgement]) -> None:
        self._ask = ask
        self._lane_for = lane_for

    async def evaluate(self, mission: Mission, handoffs: list[Handoff]) -> Verdict:
        lane = self._lane_for(mission)
        results = [(check, *run_check(check, lane.worktree)) for check in lane.checks]
        failed = tuple(check.id for check, ok, _ in results if not ok)
        scenarios = load_scenarios(lane.worktree, lane.base)
        stat, diff = _diff(lane.worktree, lane.base)
        tests = [t for check in lane.checks for t in junit_results(lane.worktree, check)]
        state = {
            "ticket": {"id": mission.ticket, "title": mission.title, "description": mission.description},
            "checks": [{"id": c.id, "passed": ok, "output_tail": tail} for c, ok, tail in results],
            "diff_stat": stat,
            "diff": diff,
            "scenarios": [text for _, text in scenarios],
            "tests": tests,  # per-test results from JUnit reports, when the repo's checks write them
        }
        questions = {"done": DONE} | {
            f"scenario_{i}": Noul(instructions={"question": "Does the change meet this scenario?", "scenario": text})
            for i, (_, text) in enumerate(scenarios)
        }
        try:
            answers = (await self._ask(state, questions)).answers
        except TypeSafeError:
            fallback = await ChecksOnlyJudge(lambda m: (lane.worktree, lane.checks)).evaluate(mission, handoffs)
            return fallback.model_copy(update={"source": "checks-only (jev unavailable)"})
        per_scenario = tuple(ScenarioResult(name=name, probability=answers[f"scenario_{i}"].noul) for i, (name, _) in enumerate(scenarios))
        done = answers["done"].noul
        # The lane is as done as its weakest scenario: that is what a send-back can name and a coder can fix.
        # Jev's whole-lane answer ran 0.1-0.2 below its own scenario scores in M3, so it is reported, not used.
        score = min((s.probability for s in per_scenario), default=done)
        notes = [f"{c.id}: {'pass' if ok else 'FAIL'}" + ("" if ok else f"\n{tail}") for c, ok, tail in results]
        weakest = sorted(per_scenario, key=lambda s: s.probability)[:3]
        notes += [f"scenario {s.probability:.2f}: {s.name}" for s in weakest]
        notes.append(f"lane done (Jev, not used for the band): {done:.2f}")
        return Verdict(score=score, source="jev", rationale="\n".join(notes), failed_checks=failed, scenarios=per_scenario)
