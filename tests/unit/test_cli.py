from codec_swarm.cli import _ask
from codec_swarm.graph import APPROVE


def _gate(verdict):
    return {"kind": "review", "after": "judge", "verdict": verdict}


def test_yes_approves_a_gate_with_clean_checks():
    assert _ask("codec-api", _gate({"failed_checks": []}), auto_approve=True) == APPROVE


def test_yes_never_approves_a_gate_with_failed_checks():
    assert _ask("codec-api", _gate({"failed_checks": ["unit"]}), auto_approve=True) is None


def test_base_flag_applies_to_every_repo_or_to_one():
    import pytest

    from codec_swarm.cli import parse_bases

    assert parse_bases(["develop"], ["api", "cli"]) == {"api": "develop", "cli": "develop"}
    assert parse_bases(["develop", "cli=release/1.2"], ["api", "cli"]) == {"api": "develop", "cli": "release/1.2"}
    with pytest.raises(SystemExit):
        parse_bases(["web=main"], ["api"])
