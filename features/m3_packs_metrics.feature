Feature: M3 Solo pack, pack choice and mission metrics
  The swarm only pays off if it cuts review time and rework, so every mission records what it cost,
  and the Solo pack gives a single-agent baseline that still keeps the judge gate.

  Scenario: The Solo pack runs one coder and the judge, with no spec gate
    Given the Solo pack and a fake backend
    And a gated mission for CODEC-1500 on codec-swarm-sandbox
    When the mission runs and I approve every gate
    Then the mission is PR ready
    And the roles ran in order: coder, judge

  Scenario Outline: Without Jev, the fixed rule picks the pack
    Given a ticket <with_description> acceptance criteria on <repos> repo(s), sensitive: <sensitive>
    When the pack is chosen without Jev
    Then the pack is <pack>, decided by rule

    Examples:
      | with_description | repos | sensitive | pack           |
      | with             | 1     | no        | solo           |
      | without          | 1     | no        | codec-standard |
      | with             | 2     | no        | codec-standard |
      | with             | 1     | yes       | codec-standard |

  Scenario: With Jev, Jev picks the pack but never Solo for a sensitive repo
    Given a ticket with acceptance criteria on 1 repo(s), sensitive: yes
    And Jev would pick solo with probability 0.90
    When the pack is chosen with Jev
    Then the pack is codec-standard, decided by rule (sensitive repo)

  Scenario: Every mission's metrics come from its event log
    Given the Codec standard pack
    And a fake backend
    And a gated mission for CODEC-1423 on codec-payment
    And the reviewer sends back its first handoff
    When the mission runs and I approve every gate
    And the metrics are computed for CODEC-1423
    Then the metrics show 8 agent steps, 1 send-back, 1 judge run and 2 gates
    And the metrics show the mission ended pr_ready

  Scenario: The report compares missions by pack and Jev
    Given recorded metrics for 2 Solo missions without Jev and 2 Codec standard missions with Jev
    When the report groups them
    Then it shows one row per pack and Jev setting with mean cost, mean time to PR and send-backs per mission
