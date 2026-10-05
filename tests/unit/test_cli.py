from codec_swarm.cli import _ask
from codec_swarm.graph import APPROVE, RunResult


def _waiting(verdict):
    return RunResult(status="waiting", gate={"kind": "review", "after": "judge", "verdict": verdict})


def test_yes_approves_a_gate_with_clean_checks():
    assert _ask(_waiting({"failed_checks": []}), auto_approve=True) == APPROVE


def test_yes_never_approves_a_gate_with_failed_checks():
    assert _ask(_waiting({"failed_checks": ["unit"]}), auto_approve=True) is None
