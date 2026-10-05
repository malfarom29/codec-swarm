Feature: M1 engine
  One lane runs through the LangGraph mission graph with a scripted fake backend.
  No tokens are spent: the fake backend and fake judge stand in for Claude Code and Jev.

  Background:
    Given the Codec standard pack
    And a fake backend

  Scenario: A gated mission stops at both gates and ends with a PR ready
    Given a gated mission for CODEC-1423 on codec-payment
    When the mission runs
    Then it waits at the spec gate
    When I approve the gate
    Then it waits at the pr gate
    When I approve the gate
    Then the mission is PR ready
    And the roles ran in order: specifier, architect, backend-coder, reviewer, hardener, qa, judge

  Scenario: The reviewer sends the work back one step
    Given a gated mission for CODEC-1423 on codec-payment
    And the reviewer sends back its first handoff
    When the mission runs and I approve every gate
    Then the mission is PR ready
    And the roles ran in order: specifier, architect, backend-coder, reviewer, backend-coder, reviewer, hardener, qa, judge

  Scenario: A verdict in the review band asks for human review
    Given a gated mission for CODEC-1423 on codec-payment
    And the judge scores 0.84
    When the mission runs
    And I approve the gate
    Then it waits at the review gate

  Scenario: A verdict below the band sends the lane back to the coder
    Given a gated mission for CODEC-1423 on codec-payment
    And the judge scores 0.60 then 0.97
    When the mission runs and I approve every gate
    Then the mission is PR ready
    And the roles ran in order: specifier, architect, backend-coder, reviewer, hardener, qa, judge, backend-coder, reviewer, hardener, qa, judge

  Scenario: A lane the judge sends back starts from the judge's verdict
    Given a gated mission for CODEC-1423 on codec-payment
    And the judge scores 0.60 then 0.97
    When the mission runs and I approve every gate
    Then the backend-coder's second step starts from the judge's handoff

  Scenario: A lane that keeps failing the judge goes to human review
    Given a gated mission for CODEC-1423 on codec-payment
    And the judge scores 0.50
    When the mission runs
    And I approve the gate
    Then it waits at the review gate
    And the judge ran 3 times

  Scenario: Auto mode opens the PR without gates when the judge approves
    Given an auto mission for CODEC-1423 on codec-payment
    When the mission runs
    Then the mission is PR ready

  Scenario: Manual mode waits after every handoff
    Given a manual mission for CODEC-1423 on codec-payment
    When the mission runs
    Then it waits at the handoff gate after the specifier

  Scenario: A crashed mission resumes where it stopped
    Given a gated mission for CODEC-1423 on codec-payment
    And the backend crashes when the hardener starts
    When the mission runs and I approve every gate
    Then the run fails while the hardener works
    When a new runner recovers the mission from the same database
    And I approve every gate
    Then the mission is PR ready
    And no role before the hardener ran twice

  Scenario: Every step lands in the event log
    Given a gated mission for CODEC-1423 on codec-payment
    When the mission runs and I approve every gate
    Then the event log has 6 handoffs, 2 opened gates, 2 resolved gates and 1 verdict
    And the event ids increase monotonically
