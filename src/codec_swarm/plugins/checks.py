"""Checks-only judge: runs the repo's hard checks in the lane worktree and gives no score."""

from __future__ import annotations

import os
import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path

from codec_swarm.domain import Handoff, Mission, Verdict
from codec_swarm.harness.config import Check

CHECK_TIMEOUT_S = 600
TAIL_LINES = 15


def run_check(check: Check, worktree: Path) -> tuple[bool, str]:
    try:
        env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}  # use the target repo's environment
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

    def __init__(self, checks_for: Callable[[Mission], tuple[Path, tuple[Check, ...]]]) -> None:
        self._checks_for = checks_for  # mission -> (lane worktree, checks)

    async def evaluate(self, mission: Mission, handoffs: list[Handoff]) -> Verdict:
        worktree, checks = self._checks_for(mission)
        failed: list[str] = []
        notes: list[str] = []
        for check in checks:
            ok, tail = run_check(check, worktree)
            notes.append(f"{check.id}: {'pass' if ok else 'FAIL'}" + ("" if ok else f"\n{tail}"))
            if not ok:
                failed.append(check.id)
        if not checks:
            notes.append("no checks configured")
        return Verdict(score=None, source="checks-only", rationale="\n".join(notes), failed_checks=tuple(failed))
