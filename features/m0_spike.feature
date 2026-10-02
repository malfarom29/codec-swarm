Feature: M0 spike
  The plan assumes how the Claude Agent SDK and the Jev SDK behave.
  This gate proves those assumptions before the adapters are written.

  Scenario: The Agent SDK has every option the harness builds
    Given the installed claude-agent-sdk
    Then ClaudeAgentOptions has every field the harness needs

  @live
  Scenario: One Haiku turn runs inside a lane worktree
    Given a throwaway git repo with a CLAUDE.md
    When a Haiku session runs one task in it with can_use_tool bridged
    Then the task's file is written inside the repo
    And every permission request went through the bridge
    And the result reports session_id, turns, tokens and cost

  @live
  Scenario: The repo's project settings load
    Given a throwaway git repo with a CLAUDE.md
    When a Haiku session runs one task in it with can_use_tool bridged
    Then the reply follows the repo's CLAUDE.md

  @live
  Scenario: A session resumes in a new process
    Given a throwaway git repo with a CLAUDE.md
    When a Haiku session runs one task in it with can_use_tool bridged
    And a new process resumes that session by session_id
    Then the resumed agent still knows the code word

  @live
  Scenario: Jev answers typed questions
    Given TYPESAFE_API_KEY is set
    When system_one asks a choice, a noul and a score question with model jev-latest
    Then each answer has the expected type and valid probabilities
    And latency and token usage are recorded
