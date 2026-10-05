Feature: M3 Jev plugins
  Jev routes models and roles, gates commands and judges lanes. Every slot falls back to its
  fixed rule when Jev is off, unsure or unavailable. A scripted fake Jev keeps these tests offline.

  Scenario: Jev is off without an API key
    Given TYPESAFE_API_KEY is not set
    When the registry picks the plugins for a mission that asks for Jev
    Then the router, command gate and judge are the fixed-rule fallbacks

  Scenario: Jev picks a cheap model but never goes below the role's floor
    Given Jev is on and would pick haiku for every role
    When the router picks the model for the specifier
    Then the model is sonnet, the specifier's floor
    And the decision source is jev

  Scenario: Jev can send work back further than one step
    Given Jev is on and would send the hardener's work to the backend-coder with probability 0.90
    When the hardener sends its work back
    Then the next role is backend-coder, decided by jev

  Scenario: An unsure next-role answer falls back to pack order
    Given Jev is on and would send the hardener's work to the backend-coder with probability 0.55
    When the hardener sends its work back
    Then the next role is reviewer, decided by rule

  Scenario: A forward handoff follows pack order without asking Jev
    Given Jev is on and would send the hardener's work to the backend-coder with probability 0.90
    When the reviewer hands off without sending back
    Then the next role is hardener, decided by rule
    And Jev was never asked

  Scenario: A Jev outage falls back to the rule for that one decision
    Given Jev is on but unavailable
    When the reviewer sends its work back
    Then the next role is backend-coder, decided by rule (jev unavailable)

  Scenario Outline: The Jev command gate runs a command only above the band
    Given the Jev command gate with threshold 0.90 and margin 0.03
    And Jev scores every command <score> safe
    When an agent asks to run "npx tsc --noEmit" in <autonomy> mode
    Then the gate answers <answer> with source <source>

    Examples:
      | score | autonomy | answer | source        |
      | 0.95  | auto     | allow  | jev           |
      | 0.91  | auto     | ask    | jev (unsure)  |
      | 0.50  | auto     | ask    | jev           |
      | 0.95  | gated    | ask    | allowlist     |

  Scenario: Jev never overrides the hard rules
    Given the Jev command gate with threshold 0.90 and margin 0.03
    And Jev scores every command 0.99 safe
    Then running "git push origin HEAD" in auto mode asks a human
    And running "python3 -c 'print(1)'" in auto mode asks a human
    And Jev was never asked

  Scenario: A script that hides a dangerous command is caught before Jev is asked
    Given the Jev command gate with threshold 0.90 and margin 0.03
    And Jev scores every command 0.99 safe
    And the repo's package.json has the script check "jest && git push origin HEAD"
    Then running "npm run check" in auto mode asks a human
    And Jev was never asked

  Scenario: The gate reuses its decision until the resolved script changes
    Given the Jev command gate with threshold 0.90 and margin 0.03
    And Jev scores every command 0.95 safe
    And the repo's package.json has the script check "eslint src"
    When an agent asks to run "npm run check" in auto mode twice
    Then Jev was asked once
    When the repo's package.json script check becomes "eslint src --fix"
    And an agent asks to run "npm run check" in auto mode
    Then Jev was asked twice

  Scenario Outline: The Jev judge scores the lane, and a failed check still blocks approval
    Given the Jev judge with the repo's checks <checks>
    And Jev says the lane is done with probability <score>
    When the judge evaluates the lane
    Then the verdict score is <score> and the band is <band>

    Examples: a lane with no approved scenarios is scored as a whole
      | checks  | score | band    |
      | passing | 0.97  | approve |
      | failing | 0.99  | review  |
      | passing | 0.85  | review  |
      | passing | 0.60  | stop    |

  Scenario: The lane's score is its weakest scenario, and a send-back names it
    Given the Jev judge with the repo's checks passing
    And the lane has 2 approved scenarios
    And Jev says the lane is done with probability 0.97
    And Jev scores scenario "Refund case 1" at 0.40
    When the judge evaluates the lane
    Then the verdict score is 0.40 and the band is stop
    And the verdict rationale names "Refund case 1"

  Scenario: The Jev judge sees which tests passed from the JUnit report
    Given a Jev judge whose passing check writes a JUnit report of 3 tests, 1 failing
    And the lane has 2 approved scenarios
    When the judge evaluates the lane
    Then Jev was shown 3 test results

  Scenario: The Jev judge checks each approved scenario
    Given the Jev judge with the repo's checks passing
    And the lane has 2 approved scenarios
    And Jev says the lane is done with probability 0.97
    When the judge evaluates the lane
    Then the verdict lists a probability for each of the 2 scenarios

  @live
  Scenario: Real Jev answers the plugins' questions
    Given TYPESAFE_API_KEY is set
    When the real Jev picks a model for the backend-coder, gates "npx tsc --noEmit" and judges a lane
    Then the model is haiku, sonnet or opus
    And the gate decision comes from jev
    And the judge's score is a probability
