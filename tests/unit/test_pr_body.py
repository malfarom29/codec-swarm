from codec_swarm.domain import Handoff, Mission, UpstreamLane
from codec_swarm.workspace.github import pr_body


def test_a_dependent_lane_pr_names_the_branch_it_depends_on():
    mission = Mission(
        ticket="CODEC-1600", repo="codec-web", title="Show refunds",
        upstream=(UpstreamLane(repo="codec-api", branch="feature/CODEC-1600-show-refunds"),),
    )
    body = pr_body(mission, [Handoff(from_role="frontend-coder", summary="Added the refunds page.")], None)
    section = body[body.index("## Depends on"):]
    assert "`codec-api` on branch `feature/CODEC-1600-show-refunds`" in section


def test_a_lane_without_upstream_has_no_depends_on_section():
    body = pr_body(Mission(ticket="CODEC-1", repo="codec-api", title="x"), [], None)
    assert "## Depends on" not in body
