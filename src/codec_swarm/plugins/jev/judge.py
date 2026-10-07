"""Jev Judge: hard checks first, then Jev's probability that each scenario and the whole lane are done."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import anyio
from pydantic import BaseModel, Field
from typesafe_sdk import Noul, TypeSafeError

from codec_swarm.domain import Handoff, Mission, ScenarioResult, Verdict
from codec_swarm.domain.models import CriterionResult
from codec_swarm.harness.spec import SpecScenario, spec_scenarios, ticket_criteria
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
    env: dict[str, str] = Field(default_factory=dict, exclude=True, repr=False)  # the repo's managed environment


def lane_spec(worktree: Path, base: str | None = None) -> list[SpecScenario]:
    """Every scenario in this lane's approved spec under .swarm/spec/, in whatever language it is written.

    Spec files already on the base branch belong to earlier missions (a squash merge can carry them in),
    so only the ones this lane added or changed count.
    """
    mine = lane_spec_files(worktree, base)
    files = [f for f in sorted((worktree / ".swarm" / "spec").glob("*.feature")) if mine is None or f.relative_to(worktree).as_posix() in mine]
    return spec_scenarios(files)


def load_scenarios(worktree: Path, base: str | None = None) -> list[tuple[str, str]]:
    return [(s.name, s.text) for s in lane_spec(worktree, base)]


def coverage(criteria: list[str], scenarios: list[SpecScenario]) -> dict[int, list[str]]:
    """For each ticket criterion (1-based), the names of the scenarios tagged @AC-n."""
    return {i: [s.name for s in scenarios if i in s.criteria] for i in range(1, len(criteria) + 1)}


def _diff(worktree: Path, base: str) -> tuple[str, str]:
    def git(*args: str) -> str:
        proc = subprocess.run(["git", *args], cwd=worktree, capture_output=True, text=True)
        return proc.stdout if proc.returncode == 0 else ""

    spec = f"origin/{base}...HEAD"
    return git("diff", "--stat", spec), git("diff", spec, "--", ".", ":(exclude).swarm")[:MAX_DIFF_CHARS]


def _gather(lane: LaneUnderJudgement) -> tuple[list[tuple[Check, bool, str]], list[SpecScenario], tuple[str, str], list[dict[str, object]]]:
    results = [(check, *run_check(check, lane.worktree, lane.env)) for check in lane.checks]
    tests = [t for check in lane.checks for t in junit_results(lane.worktree, check)]
    return results, lane_spec(lane.worktree, lane.base), _diff(lane.worktree, lane.base), tests


class JevJudge:
    def __init__(self, ask: SystemOne, lane_for: Callable[[Mission], LaneUnderJudgement]) -> None:
        self._ask = ask
        self._lane_for = lane_for

    async def evaluate(self, mission: Mission, handoffs: list[Handoff]) -> Verdict:
        lane = self._lane_for(mission)
        results, scenarios, (stat, diff), tests = await anyio.to_thread.run_sync(_gather, lane)  # checks and git block
        failed = tuple(check.id for check, ok, _ in results if not ok)
        criteria = ticket_criteria(mission.description)
        covered = coverage(criteria, scenarios)
        uncovered = [i for i, names in covered.items() if not names]  # no scenario says it covers these: judge them directly
        state = {
            "ticket": {"id": mission.ticket, "title": mission.title, "description": mission.description},
            "checks": [{"id": c.id, "passed": ok, "output_tail": tail} for c, ok, tail in results],
            "diff_stat": stat,
            "diff": diff,
            "scenarios": [s.text for s in scenarios],
            "acceptance_criteria": [f"AC-{i}: {text}" for i, text in enumerate(criteria, 1)],
            "tests": tests,  # per-test results from JUnit reports, when the repo's checks write them
        }
        questions = {"done": DONE} | {
            f"scenario_{i}": Noul(instructions={"question": "Does the change meet this scenario?", "scenario": s.text})
            for i, s in enumerate(scenarios)
        } | {
            f"criterion_{i}": Noul(instructions={"question": "Does the change meet this acceptance criterion from the ticket?", "criterion": criteria[i - 1]})
            for i in uncovered
        }
        try:
            answers = (await self._ask(state, questions)).answers
        except TypeSafeError:
            fallback = await ChecksOnlyJudge(lambda m: (lane.worktree, lane.checks, lane.env)).evaluate(mission, handoffs)
            return fallback.model_copy(update={"source": "checks-only (jev unavailable)"})
        per_scenario = tuple(
            ScenarioResult(name=s.name, probability=answers[f"scenario_{i}"].noul, criteria=s.criteria) for i, s in enumerate(scenarios)
        )
        by_name = {s.name: s.probability for s in per_scenario}
        per_criterion = tuple(
            CriterionResult(
                index=i, text=criteria[i - 1], covered_by=tuple(covered[i]),
                probability=answers[f"criterion_{i}"].noul if i in uncovered else min(by_name[n] for n in covered[i]),
            )
            for i in range(1, len(criteria) + 1)
        )
        done = answers["done"].noul
        # The lane is as done as its weakest scenario, or its weakest ticket criterion that no scenario covers:
        # that is what a send-back can name and a coder can fix. Jev's whole-lane answer ran 0.1-0.2 below its own
        # scenario scores in M3, so it is reported, not used.
        judged = [s.probability for s in per_scenario] + [c.probability for c in per_criterion if c.index in uncovered]
        score = min(judged, default=done)
        notes = [f"{c.id}: {'pass' if ok else 'FAIL'}" + ("" if ok else f"\n{tail}") for c, ok, tail in results]
        weakest = sorted(per_scenario, key=lambda s: s.probability)[:3]
        notes += [f"scenario {s.probability:.2f}: {s.name}" for s in weakest]
        notes += [f"criterion AC-{c.index} {c.probability:.2f} (no scenario covers it): {c.text}" for c in per_criterion if c.index in uncovered]
        notes.append(f"lane done (Jev, not used for the band): {done:.2f}")
        return Verdict(score=score, source="jev", rationale="\n".join(notes), failed_checks=failed, scenarios=per_scenario, criteria=per_criterion)
