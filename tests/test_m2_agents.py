"""Step definitions for features/m2_agents.feature. Only the @live scenario calls Claude Code."""

from pathlib import Path

import anyio
import pytest
import yaml
from pytest_bdd import given, parsers, scenarios, then, when

from codec_swarm.domain import Autonomy, Band, Handoff, JudgeBands, Mission
from codec_swarm.harness import BranchFlow, Check, LocalOverrides, MissionExtras, load_pack, load_repo_config, resolve_session
from codec_swarm.plugins.api import StepRequest
from codec_swarm.plugins.checks import ChecksOnlyJudge
from codec_swarm.plugins.claude_code import ClaudeCodeBackend
from codec_swarm.plugins.gate import AllowlistGate, GateContext
from codec_swarm.store import SessionStore
from codec_swarm.workspace import Workspace, git
from codec_swarm.workspace.github import GitHubPublisher

scenarios("../features/m2_agents.feature")

TEST_PACK = Path(__file__).parent / "fixtures" / "packs" / "test-pack"


@pytest.fixture
def world(tmp_path):
    return {"tmp": tmp_path}


def _commit(repo: Path, message: str) -> None:
    git(repo, "-c", "user.name=test", "-c", "user.email=test@localhost", "commit", "--quiet", "-m", message)


# --- harness ---------------------------------------------------------------


@given("the Codec standard pack from packs/codec-standard")
def codec_pack(world):
    world["pack"] = load_pack("codec-standard")


@given(parsers.parse("a nestjs repo whose config adds the {server} MCP server for the {role}"))
def repo_with_config(world, server, role):
    repo = world["tmp"] / "repo"
    (repo / ".swarm").mkdir(parents=True)
    config = {"version": 1, "stack": "nestjs", "roles": {role: {"extra_mcp": [server]}}}
    (repo / ".swarm" / "config.yaml").write_text(yaml.safe_dump(config))
    world["repo"] = repo


@given("a nestjs repo with no config")
def repo_without_config(world):
    world["repo"] = world["tmp"] / "repo"
    world["repo"].mkdir()
    world["stack"] = "nestjs"


def _resolve(world, role, extras=MissionExtras()):
    config = load_repo_config(world["repo"], world["pack"], stack=world.get("stack"))
    mission = Mission(ticket="CODEC-1423", repo="codec-payment", title="Partial refunds")
    world["spec"] = resolve_session(world["pack"], config, role, world["repo"], mission, extras)


@when(parsers.parse("the harness resolves the {role} session for lane CODEC-1423 on codec-payment"))
def resolves(world, role):
    _resolve(world, role)


@when(parsers.parse("the harness resolves the {role} session with the mission extra MCP server {server}"))
def resolves_with_extra(world, role, server):
    _resolve(world, role, MissionExtras(mcp=(server,)))


@then(parsers.parse("the session runs in the lane worktree with model {model}"))
def runs_in_worktree(world, model):
    assert world["spec"].cwd == world["repo"]
    assert world["spec"].model == model


@then("the system prompt holds the core, stack and role layers in that order")
def layers_in_order(world):
    prompt = world["spec"].system_prompt
    positions = [prompt.index(h) for h in ("# Layer 1 · Core", "# Layer 3 · Stack: NestJS", "# Layer 4 · Role: Backend coder", "# Layer 5 · Mission")]
    assert positions == sorted(positions)


@then(parsers.parse("the session MCP servers are {servers}"))
def mcp_servers(world, servers):
    assert list(world["spec"].mcp_servers) == [s.strip() for s in servers.replace(" and ", ",").split(",")]


@then("no tool is pre-approved")
def no_preapproved(world):
    assert world["spec"].allowed_tools == ()


@then("the reviewer still loads the code-review and security-review skills")
def reviewer_skills(world):
    assert {"code-review", "security-review"} <= set(world["spec"].skills)


@then("MCP secrets are still ${env:...} references")
def secrets_unresolved(world):
    assert world["spec"].mcp_servers["sentry"]["headers"]["Authorization"] == "Bearer ${env:SENTRY_TOKEN}"


# --- workspace -------------------------------------------------------------


@given("a local origin repo with a develop branch")
def origin_repo(world):
    seed = world["tmp"] / "seed"
    seed.mkdir()
    git(seed, "init", "--quiet", "-b", "develop")
    (seed / "README.md").write_text("# codec-payment\n")
    git(seed, "add", "README.md")
    _commit(seed, "chore: initial commit")
    git(world["tmp"], "clone", "--quiet", "--bare", str(seed), "origin.git")
    world["origin"] = str(world["tmp"] / "origin.git")
    world["workspace"] = Workspace(world["tmp"] / "codec-swarm")


@when(parsers.parse('the workspace prepares lane {ticket} "{title}"'))
@given(parsers.parse('the workspace prepared lane {ticket} "{title}"'))
def prepares_lane(world, ticket, title):
    world["lane"] = world["workspace"].prepare_lane(world["origin"], "codec-payment", ticket, title, BranchFlow(base="develop"))
    world["title"] = title


@then(parsers.parse("the lane worktree is on branch {branch}"))
def on_branch(world, branch):
    assert git(world["lane"].path, "branch", "--show-current") == branch


@then("the branch starts from origin/develop")
def from_develop(world):
    assert git(world["lane"].path, "rev-parse", "HEAD") == git(world["lane"].path, "rev-parse", "origin/develop")


@given("the backend-coder changed src/refunds.ts")
def changed_file(world):
    (world["lane"].path / "src").mkdir()
    (world["lane"].path / "src" / "refunds.ts").write_text("export const refund = () => {};\n")


@given("the backend-coder also left verify_mission.py")
def scratch_file(world):
    (world["lane"].path / "verify_mission.py").write_text("print('checking')\n")


@then("the handoff file flags verify_mission.py as a new file it did not list")
def handoff_flags(world):
    text = (_record(world) / "handoffs" / "01-backend-coder-reviewer.md").read_text()
    section = text[text.index("## New files not listed"):]
    assert "`verify_mission.py`" in section and "refunds.ts" not in section


@then("the PR body flags verify_mission.py as a file no handoff listed")
def pr_body_flags(world):
    from codec_swarm.workspace.github import pr_body

    handoff = Handoff(from_role="backend-coder", summary="Added POST /refunds.", files_touched=("src/refunds.ts",))
    body = pr_body(Mission(ticket="CODEC-1423", repo="codec-payment", title="Partial refunds"), [handoff], None, world["lane"])
    section = body[body.index("## Files no handoff listed"):]
    assert "`verify_mission.py`" in section and "refunds.ts" not in section


@when(parsers.parse('the backend-coder hands off to the reviewer with commit message "{message}"'))
def hands_off(world, message):
    handoff = Handoff(
        from_role="backend-coder",
        summary="Added POST /refunds with an idempotency key.",
        commit_message=message,
        files_touched=("src/refunds.ts",),
    )
    world["sha"] = world["workspace"].record_handoff(world["lane"], 1, handoff, "reviewer")


def _record(world):
    return world["workspace"].record_dir(world["lane"].ticket, world["lane"].repo)


@given("the specifier wrote .swarm/spec/refunds.feature")
def wrote_spec(world):
    spec = world["lane"].path / ".swarm" / "spec"
    spec.mkdir(parents=True)
    (spec / "refunds.feature").write_text("Feature: Refunds\n\n  Scenario: Partial refund\n    Then it is refunded\n")


@then("the mission record holds the handoff as markdown")
def handoff_file(world):
    text = (_record(world) / "handoffs" / "01-backend-coder-reviewer.md").read_text()
    assert text.startswith("# Handoff: backend-coder → reviewer")
    assert "`src/refunds.ts`" in text


@then(parsers.parse("the mission record keeps the file {path}"))
def record_holds(world, path):
    assert (_record(world) / path).is_file()


@then(parsers.parse('the lane\'s last commit is "{code}" and the branch has no .swarm files'))
def last_commit(world, code):
    assert git(world["lane"].path, "log", "-1", "--format=%s") == code
    assert not [p for p in git(world["lane"].path, "ls-files").splitlines() if p.startswith(".swarm/")]
    assert git(world["lane"].path, "status", "--porcelain") == ""


# --- command gate ----------------------------------------------------------


@given(parsers.parse('the command gate with the repo allowlist "{allowlist}"'))
def command_gate(world, allowlist):
    world["gate"] = AllowlistGate(tuple(allowlist.split(",")))
    world["ctx"] = GateContext(ticket="CODEC-1423", role="backend-coder", worktree=world["tmp"], autonomy=Autonomy.AUTO)


@when(parsers.parse('an agent asks to run "{command}" in Auto mode'))
def asks_to_run(world, command):
    world["decision"] = world["gate"].decide("Bash", {"command": command}, world["ctx"])


@then(parsers.parse("the gate answers {answer}"))
def gate_answers(world, answer):
    assert world["decision"].action.value == answer, world["decision"]


@then("the gate asks a human because it runs inline code")
def asks_inline(world):
    assert world["decision"].action.value == "ask"
    assert world["decision"].source == "always-ask"
    assert world["decision"].reason == "runs inline code"


@then(parsers.parse('running "{command}" is allowed'))
def running_allowed(world, command):
    assert world["gate"].decide("Bash", {"command": command}, world["ctx"]).action.value == "allow"


@then(parsers.parse('running "{command}" asks a human'))
def running_asks(world, command):
    assert world["gate"].decide("Bash", {"command": command}, world["ctx"]).action.value == "ask"


@then(parsers.parse('the system prompt lists "{first}" and "{second}" as commands that run without asking'))
def prompt_lists_commands(world, first, second):
    prompt = world["spec"].system_prompt
    section = prompt[prompt.index("# Commands you can run without asking"):]
    assert f"`{first}`" in section and f"`{second}`" in section


@then(parsers.parse('reading "{path}" is {verdict}'))
def reading(world, path, verdict):
    action = world["gate"].decide("Read", {"file_path": path}, world["ctx"]).action.value
    assert action == {"allowed": "allow", "denied": "deny"}[verdict]


@then(parsers.parse('writing "{path}" is {verdict}'))
def writing(world, path, verdict):
    action = world["gate"].decide("Write", {"file_path": path, "content": "x"}, world["ctx"]).action.value
    assert action == {"allowed": "allow", "denied": "deny"}[verdict]


# --- judge and publisher ---------------------------------------------------


@when("the checks-only judge runs a passing check and a failing check")
def checks_judge(world):
    checks = (Check(id="unit", run="python3 -c 'print(42)'"), Check(id="lint", run="python3 -c 'raise SystemExit(1)'"))
    judge = ChecksOnlyJudge(lambda m: (world["lane"].path, checks))
    mission = Mission(ticket="CODEC-1423", repo="codec-payment")
    world["verdict"] = anyio.run(judge.evaluate, mission, [])


@then("the verdict has no score and lists the failing check")
def verdict_failed(world):
    assert world["verdict"].score is None
    assert world["verdict"].failed_checks == ("lint",)


@then("the lane goes back to the coder")
def goes_back(world):
    assert JudgeBands().classify(world["verdict"]) is Band.STOP


class FakeGh:
    """Stands in for the gh CLI: records calls and remembers the PR it 'opened'."""

    def __init__(self):
        self.calls = []
        self.url = ""

    def __call__(self, argv, cwd):
        self.calls.append(list(argv))
        if argv[:3] == ["gh", "pr", "list"]:
            return self.url
        if argv[:3] == ["gh", "pr", "create"]:
            self.url = "https://github.com/example/codec-payment/pull/1"
            return self.url
        raise AssertionError(f"unexpected command {argv}")


@when("the publisher opens the lane's PR")
def publishes(world):
    world["gh"] = FakeGh()
    world["publisher"] = GitHubPublisher({("CODEC-1423", "codec-payment"): world["lane"]}, run=world["gh"])
    world["mission"] = Mission(ticket="CODEC-1423", repo="codec-payment", title="Partial refunds")
    handoffs = [Handoff(from_role="backend-coder", summary="Added POST /refunds.")]
    world["pr_url"] = world["publisher"].publish(world["mission"], handoffs, {"source": "checks-only", "band": "review", "rationale": "unit: pass"})


@then("origin has the lane branch")
def origin_has_branch(world):
    assert git(world["tmp"] / "origin.git", "branch", "--list", world["lane"].branch)


@then("gh was asked to open a PR from the lane branch into develop")
def gh_create_args(world):
    create = next(c for c in world["gh"].calls if c[:3] == ["gh", "pr", "create"])
    assert create[create.index("--base") + 1] == "develop"
    assert create[create.index("--head") + 1] == world["lane"].branch
    assert create[create.index("--title") + 1] == "CODEC-1423: Partial refunds"


@then("publishing again returns the same PR without a second gh pr create")
def idempotent(world):
    again = world["publisher"].publish(world["mission"], [], None)
    assert again == world["pr_url"]
    assert sum(c[:3] == ["gh", "pr", "create"] for c in world["gh"].calls) == 1


# --- live Claude Code step -------------------------------------------------


@when("the backend-coder runs one real step with Haiku")
def real_step(world):
    pack = load_pack(TEST_PACK)
    lane = world["lane"]
    mission = Mission(ticket=lane.ticket, repo=lane.repo, title=world["title"], autonomy=Autonomy.AUTO)
    config = load_repo_config(lane.path, pack, stack="python")
    world["sessions"] = SessionStore(world["tmp"] / "swarm.db")

    def session_for(m, role):
        return resolve_session(pack, config, role, lane.path, m, overrides=LocalOverrides(model="haiku"))

    backend = ClaudeCodeBackend(session_for, AllowlistGate(config.allowlist), world["sessions"])

    async def collect():
        return [e async for e in backend.run_step(StepRequest(mission=mission, role="backend-coder"))]

    world["mission"] = mission
    world["events"] = anyio.run(collect)


@then("the step ends with a handoff event from the backend-coder")
def ends_with_handoff(world):
    last = world["events"][-1]
    assert last.kind == "handoff" and last.role == "backend-coder"
    world["handoff"] = Handoff.model_validate(last.payload)
    assert world["handoff"].summary


@then("the lane stores the session id for the backend-coder")
def stores_session(world):
    assert world["sessions"].get(world["mission"].ticket, world["mission"].repo, "backend-coder")


@then("the orchestrator commits HEALTH.md and the handoff")
def health_committed(world):
    world["workspace"].record_handoff(world["lane"], 1, world["handoff"], "reviewer")
    assert git(world["lane"].path, "log", "-1", "--format=%s") == "chore(swarm): backend-coder handoff"
    assert "HEALTH.md" in git(world["lane"].path, "log", "-1", "--skip=1", "--name-only", "--format=")
    assert world["handoff"].commit_message
