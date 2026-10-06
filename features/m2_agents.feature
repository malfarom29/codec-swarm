Feature: M2 real agents
  The fake backend is replaced by Claude Code sessions that run in real worktrees,
  load their harness from the pack and hand off through committed files.

  Scenario: The harness builds a role's session from the pack and the repo config
    Given the Codec standard pack from packs/codec-standard
    And a nestjs repo whose config adds the playwright MCP server for the backend-coder
    When the harness resolves the backend-coder session for lane CODEC-1423 on codec-payment
    Then the session runs in the lane worktree with model sonnet
    And the system prompt holds the core, stack and role layers in that order
    And the session MCP servers are context7 and playwright
    And no tool is pre-approved

  Scenario: A mission may add capabilities but never remove the pack's
    Given the Codec standard pack from packs/codec-standard
    And a nestjs repo with no config
    When the harness resolves the reviewer session with the mission extra MCP server sentry
    Then the session MCP servers are sentry
    And the reviewer still loads the code-review and security-review skills
    And MCP secrets are still ${env:...} references

  Scenario: Each lane gets its own worktree on the branch-flow branch
    Given a local origin repo with a develop branch
    When the workspace prepares lane CODEC-1423 "Partial refunds from the payment link"
    Then the lane worktree is on branch feature/CODEC-1423-partial-refunds-from-the-payment-link
    And the branch starts from origin/develop

  Scenario: New files no handoff listed are flagged
    Given a local origin repo with a develop branch
    And the workspace prepared lane CODEC-1423 "Partial refunds"
    And the backend-coder changed src/refunds.ts
    And the backend-coder also left verify_mission.py
    When the backend-coder hands off to the reviewer with commit message "feat(refunds): add idempotency key"
    Then the handoff file flags verify_mission.py as a new file it did not list
    And the PR body flags verify_mission.py as a file no handoff listed

  Scenario: The orchestrator commits the step's code and keeps the handoff and spec out of the branch
    Given a local origin repo with a develop branch
    And the workspace prepared lane CODEC-1423 "Partial refunds"
    And the specifier wrote .swarm/spec/refunds.feature
    And the backend-coder changed src/refunds.ts
    When the backend-coder hands off to the reviewer with commit message "feat(refunds): add idempotency key"
    Then the mission record holds the handoff as markdown
    And the mission record keeps the file spec/refunds.feature
    And the lane's last commit is "feat(refunds): add idempotency key" and the branch has no .swarm files

  Scenario Outline: Without Jev, the command gate runs only the allowlist in Auto mode
    Given the command gate with the repo allowlist "npm run test, npm run lint"
    When an agent asks to run "<command>" in Auto mode
    Then the gate answers <answer>

    Examples:
      | command                     | answer |
      | npm run test                | allow  |
      | git status                  | allow  |
      | git commit -m wip           | ask    |
      | npm run lint -- --fix       | allow  |
      | npm run test && git push    | ask    |
      | git push origin HEAD        | ask    |
      | npx prisma migrate deploy   | ask    |
      | npm publish                 | ask    |
      | cat ../../other-repo/.env   | ask    |
      | npm run deploy              | ask    |

  Scenario Outline: Inline code always needs a human, whatever the allowlist or Jev says
    Given the command gate with the repo allowlist "python3, node, bash"
    When an agent asks to run "<command>" in Auto mode
    Then the gate asks a human because it runs inline code

    Examples:
      | command                           |
      | python3 -c "import os; print(1)"  |
      | python3 << 'EOF'                  |
      | node -e "require('fs')"           |
      | bash -c "rm -rf build"            |
      | eval "$CMD"                       |

  Scenario: Read-only output filters keep an allowlisted command allowed
    Given the command gate with the repo allowlist "uv run pytest"
    Then running "uv run pytest -q 2>&1 | tail -20" is allowed
    And running "uv run pytest | grep FAILED" is allowed
    And running "uv run pytest | sh" asks a human
    And running "uv run pytest > out.txt" asks a human
    And running "uv run pytest 2>&1 | tail -20 && git push" asks a human

  Scenario: Safe commands from the first real missions run without asking
    Given the command gate with the repo allowlist "uv run pytest"
    Then running "uv run pytest -v | grep -E "(PASSED|FAILED)"" is allowed
    And running "uv run pytest && git status" is allowed
    And running "git config --list | grep -i jira" is allowed
    And running "uv pip list | grep -i mut" is allowed
    And running "mkdir -p .swarm/spec" is allowed
    And running "rm verify_mission.py && git status" is allowed
    And running "rm -rf ../other-repo" asks a human
    And running "rm -rf ." asks a human
    And running "rm -rf .git" asks a human
    And running "uv run pytest && npm publish" asks a human
    And running "echo done && grep -c test_ tests/test_cents.py" is allowed
    And running "cat ../../other-repo/.env" asks a human
    And running "echo secret > notes.txt" asks a human

  Scenario: Agents are told which commands run without asking
    Given the Codec standard pack from packs/codec-standard
    And a nestjs repo with no config
    When the harness resolves the backend-coder session for lane CODEC-1423 on codec-payment
    Then the system prompt lists "npm run test" and "git status" as commands that run without asking

  Scenario: File tools cannot leave the lane worktree
    Given the command gate with the repo allowlist "npm run test"
    Then reading "src/refunds.ts" is allowed
    And reading "../other-repo/.env" is denied
    And writing "/etc/hosts" is denied

  Scenario: The checks-only judge runs the repo's checks in the lane
    Given a local origin repo with a develop branch
    And the workspace prepared lane CODEC-1423 "Partial refunds"
    When the checks-only judge runs a passing check and a failing check
    Then the verdict has no score and lists the failing check
    And the lane goes back to the coder

  Scenario: A judged lane becomes a local PR with one commit and one description
    Given a local origin repo with a develop branch
    And the workspace prepared lane CODEC-1423 "Partial refunds"
    And two steps committed code, the last describing the change as "Refunds can now be partial and carry an idempotency key."
    When the publisher prepares the lane's local PR
    Then the lane branch has one commit "feat: Partial refunds" on top of develop
    And the PR description says "Refunds can now be partial and carry an idempotency key." and lists no roles
    And origin does not have the lane branch

  Scenario: Pushing a local PR opens it on GitHub into the branch I pick
    Given a local origin repo with a develop branch
    And the workspace prepared lane CODEC-1423 "Partial refunds"
    And two steps committed code, the last describing the change as "Refunds can now be partial."
    And the publisher prepared the lane's local PR
    When I push the local PR into release/2026.10
    Then origin has the lane branch
    And gh was asked to open a PR from the lane branch into release/2026.10
    And pushing again returns the same PR without a second gh pr create

  Scenario: Merging a local PR puts the change on a local branch and pushes nothing
    Given a local origin repo with a develop branch
    And the workspace prepared lane CODEC-1423 "Partial refunds"
    And two steps committed code, the last describing the change as "Refunds can now be partial."
    And the publisher prepared the lane's local PR
    When I merge the local PR into develop locally
    Then the clone's develop has src/refunds.ts
    And origin's develop does not have src/refunds.ts
    And merging it again is refused

  @live
  Scenario: A real Claude Code step ends with a structured handoff
    Given a local origin repo with a develop branch
    And the workspace prepared lane CODEC-1999 "Add a health check"
    When the backend-coder runs one real step with Haiku
    Then the step ends with a handoff event from the backend-coder
    And the lane stores the session id for the backend-coder
    And the orchestrator commits HEALTH.md and the handoff
