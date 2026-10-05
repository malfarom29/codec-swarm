Feature: M3 multi-lane missions
  One ticket can touch several repos. Planning runs once for the mission; then each repo gets its
  own lane, started in the dependency order the Architect plans, so a client lane codes against a
  real API branch. Each lane has its own gates, judge and PR.

  Background:
    Given the Codec standard pack
    And a fake backend

  Scenario: A client lane starts only after its API lane reaches the judge
    Given a gated mission for CODEC-1600 on codec-api and codec-web
    And the architect orders codec-web after codec-api
    When the mission runs
    Then the mission waits at the spec gate
    When I approve the spec gate
    Then lane codec-api waits at the pr gate
    And lane codec-web waits at the pr gate
    And lane codec-web started after lane codec-api reached the judge

  Scenario: A dependent lane is told where its upstream lane's pushed branch is
    Given a gated mission for CODEC-1606 on codec-api and codec-web
    And the architect orders codec-web after codec-api
    When the mission runs
    And I approve the spec gate
    Then codec-api's branch was pushed before lane codec-web started
    And every step of lane codec-web knew codec-api's pushed branch
    And lane codec-api's steps were told of no upstream lane

  Scenario: Independent lanes start together after the spec gate
    Given a gated mission for CODEC-1601 on codec-api and codec-web
    And the architect gives no lane order
    When the mission runs
    And I approve the spec gate
    Then lane codec-api waits at the pr gate
    And lane codec-web waits at the pr gate
    And both lanes started right after the spec gate

  Scenario: Each lane's gates are answered separately
    Given a gated mission for CODEC-1602 on codec-api and codec-web
    And the architect orders codec-web after codec-api
    When the mission runs
    And I approve the spec gate
    And I approve the pr gate of lane codec-api
    Then lane codec-api is PR ready
    And lane codec-web waits at the pr gate
    When I approve the pr gate of lane codec-web
    Then every lane is PR ready

  Scenario: A lane that waits on a lane that never reaches the judge does not start
    Given a gated mission for CODEC-1603 on codec-api and codec-web
    And the architect orders codec-web after codec-api
    And the backend crashes when the backend-coder starts in codec-api
    When the mission runs
    And I approve the spec gate
    Then lane codec-api failed
    And lane codec-web has not started

  Scenario: A lane order with a cycle is rejected at planning
    Given a gated mission for CODEC-1604 on codec-api and codec-web
    And the architect orders codec-web after codec-api and codec-api after codec-web
    When the mission runs
    And I approve the spec gate
    Then the mission is blocked because the lane order has a cycle

  Scenario: The single-repo engine still runs one lane end to end
    Given a gated mission for CODEC-1605 on codec-api
    When the mission runs
    And I approve the spec gate
    And I approve the pr gate of lane codec-api
    Then every lane is PR ready
