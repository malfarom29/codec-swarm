"""Checks-only judge: runs the repo's hard checks in the lane worktree and gives no score."""

from __future__ import annotations

import os
import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio

from codec_swarm.domain import Handoff, Mission, ScenarioResult, Verdict
from codec_swarm.domain.models import CriterionResult
from codec_swarm.harness.config import Check
from codec_swarm.harness.spec import spec_scenarios, ticket_criteria

CHECK_TIMEOUT_S = 600
TAIL_LINES = 15


def run_check(check: Check, worktree: Path, extra_env: dict[str, str] | None = None) -> tuple[bool, str]:
    """Blocking; async callers run it in a worker thread so a long test run never stalls the event loop."""
    try:
        env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}  # use the target repo's environment
        env.update(extra_env or {})  # the repo's managed environment wins over the server's
        proc = subprocess.run(check.run, shell=True, cwd=worktree, env=env, capture_output=True, text=True, timeout=CHECK_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return False, f"timed out after {CHECK_TIMEOUT_S}s"
    output = (proc.stdout + proc.stderr).strip().splitlines()
    return proc.returncode == 0, "\n".join(output[-TAIL_LINES:])


MAX_TESTS = 300


def junit_results(worktree: Path, check: Check) -> list[dict[str, object]]:
    """Per-test results from a check's JUnit report, if it has one: the judge's executable evidence."""
    if check.report is None or check.report.kind != "junit":
        return []
    path = worktree / check.report.path
    if not path.exists():
        return []
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return []
    results = []
    for case in root.iter("testcase"):
        failed = any(child.tag in ("failure", "error") for child in case)
        skipped = any(child.tag == "skipped" for child in case)
        results.append({"test": f"{case.get('classname', '')}::{case.get('name', '')}", "passed": not failed and not skipped, "skipped": skipped})
    return results[:MAX_TESTS]


class ChecksOnlyJudge:
    """Fallback Judge when Jev is off: a failed check sends the lane back, a clean run goes to a human."""

    def __init__(self, checks_for: Callable[[Mission], tuple[Any, ...]]) -> None:
        self._checks_for = checks_for  # mission -> (lane worktree, checks) or (worktree, checks, env)

    async def evaluate(self, mission: Mission, handoffs: list[Handoff]) -> Verdict:
        worktree, checks, *rest = self._checks_for(mission)
        env = rest[0] if rest else None
        failed: list[str] = []
        notes: list[str] = []
        for check in checks:
            ok, tail = await anyio.to_thread.run_sync(run_check, check, worktree, env)
            notes.append(f"{check.id}: {'pass' if ok else 'FAIL'}" + ("" if ok else f"\n{tail}"))
            if not ok:
                failed.append(check.id)
        if not checks:
            notes.append("no checks configured")
        # No scores without Jev, but the Definition of Done still lists the spec and which ticket criteria it covers.
        scenarios = spec_scenarios(sorted((Path(worktree) / ".swarm" / "spec").glob("*.feature")))
        criteria = ticket_criteria(mission.description)
        listed = tuple(ScenarioResult(name=s.name, criteria=s.criteria) for s in scenarios)
        covered = tuple(
            CriterionResult(index=i, text=text, covered_by=tuple(s.name for s in scenarios if i in s.criteria))
            for i, text in enumerate(criteria, 1)
        )
        return Verdict(score=None, source="checks-only", rationale="\n".join(notes), failed_checks=tuple(failed), scenarios=listed, criteria=covered)
