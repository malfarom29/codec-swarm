import pytest
from pydantic import ValidationError

from codec_swarm.domain import CODEC_STANDARD, Band, Handoff, JudgeBands, Verdict
from codec_swarm.plugins.fallback import PackOrderRouter


class TestJudgeBands:
    bands = JudgeBands(approve_at=0.95, review_at=0.80)

    @pytest.mark.parametrize(
        ("score", "band"),
        [(0.97, Band.APPROVE), (0.95, Band.APPROVE), (0.94, Band.REVIEW), (0.80, Band.REVIEW), (0.79, Band.STOP)],
    )
    def test_score_lands_in_its_band(self, score, band):
        assert self.bands.classify(Verdict(score=score, source="test")) is band

    def test_no_score_with_passing_checks_always_needs_a_human(self):
        assert self.bands.classify(Verdict(score=None, source="checks-only")) is Band.REVIEW

    def test_no_score_with_a_failed_check_sends_the_lane_back(self):
        verdict = Verdict(score=None, source="checks-only", failed_checks=("unit",))
        assert self.bands.classify(verdict) is Band.STOP

    def test_a_failed_hard_check_blocks_auto_approval(self):
        verdict = Verdict(score=0.99, source="jev", failed_checks=("mutation",))
        assert self.bands.classify(verdict) is Band.REVIEW

    def test_review_threshold_cannot_exceed_approve_threshold(self):
        with pytest.raises(ValidationError):
            JudgeBands(approve_at=0.80, review_at=0.90)

    def test_a_mission_may_tighten_but_never_loosen(self):
        tighter = self.bands.tightened(JudgeBands(approve_at=0.90, review_at=0.85))
        assert (tighter.approve_at, tighter.review_at) == (0.95, 0.85)


class TestPack:
    def test_codec_standard_roles(self):
        assert CODEC_STANDARD.planning_roles == ("specifier", "architect")
        assert CODEC_STANDARD.lane_roles == ("backend-coder", "reviewer", "hardener", "qa")
        assert CODEC_STANDARD.rework_role == "backend-coder"

    def test_navigation(self):
        assert CODEC_STANDARD.next_role("specifier") == "architect"
        assert CODEC_STANDARD.next_role("architect") is None
        assert CODEC_STANDARD.next_role("hardener") == "qa"
        assert CODEC_STANDARD.next_role("qa") is None
        assert CODEC_STANDARD.previous_lane_role("reviewer") == "backend-coder"
        assert CODEC_STANDARD.previous_lane_role("backend-coder") is None


class TestPackOrderRouter:
    router = PackOrderRouter()

    def _route(self, role, send_back=False):
        return self.router.next_role(CODEC_STANDARD, role, Handoff(from_role=role, summary="", send_back=send_back))

    def test_planning_roles_lead_to_the_spec_gate(self):
        assert self._route("specifier").next == "architect"
        assert self._route("architect").next == "spec_gate"

    def test_lane_roles_go_forward_then_to_the_judge(self):
        assert self._route("reviewer").next == "hardener"
        assert self._route("qa").next == "judge"

    def test_send_back_goes_one_step_only(self):
        decision = self._route("hardener", send_back=True)
        assert decision.next == "reviewer"
        assert decision.source == "rule"

    def test_the_first_lane_role_cannot_send_back_further(self):
        assert self._route("backend-coder", send_back=True).next == "backend-coder"
