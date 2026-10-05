Feature: M4 dashboard
  `codec-swarm up` serves a server-rendered dashboard (FastAPI, Jinja, htmx) on localhost.
  Everything it shows comes from the event log, and everything it does goes through the mission
  service, so the browser and the CLI drive missions the same way. A fake service keeps this offline.

  Background:
    Given the dashboard runs with a fake mission service and launch token "s3cret"

  Scenario: Without the launch token the dashboard refuses every page
    When I open "/" without the token
    Then the response is 401

  Scenario: The token in the launch URL becomes a cookie
    When I open "/?token=s3cret"
    Then I am redirected to "/" with a session cookie
    And opening "/" with that cookie shows the mission board

  Scenario: Starting a mission from the form runs it to its first gate
    Given I am signed in
    When I start mission CODEC-1700 "Add from_cents" on codec-swarm-sandbox in gated mode
    Then the mission board lists CODEC-1700 under spec
    And the inbox shows the spec gate of CODEC-1700

  Scenario: Approving gates from the inbox takes a mission to PR ready
    Given I am signed in
    And mission CODEC-1701 "Add from_cents" on codec-swarm-sandbox is waiting at its spec gate
    When I approve the spec gate of CODEC-1701 from the inbox
    Then the inbox shows the pr gate of CODEC-1701 for lane codec-swarm-sandbox
    When I approve the pr gate of CODEC-1701 for lane codec-swarm-sandbox
    Then the inbox is empty
    And the mission board lists CODEC-1701 under pr ready

  Scenario: The mission page shows each lane, its verdicts and its agents' output
    Given I am signed in
    And mission CODEC-1702 "Add from_cents" on codec-swarm-sandbox is waiting at its pr gate
    When I open the mission page of CODEC-1702
    Then it shows lane codec-swarm-sandbox waiting at the pr gate
    And it shows the judge's verdict for lane codec-swarm-sandbox
    And it shows the agent output of lane codec-swarm-sandbox

  Scenario: The board by agent shows a lane at a gate under Waiting on you
    Given I am signed in
    And mission CODEC-1704 "Add from_cents" on codec-swarm-sandbox is waiting at its pr gate
    When I open "/?view=agents"
    Then the Waiting on you column holds lane codec-swarm-sandbox of CODEC-1704
    And the board shows 1 gate waiting on me

  Scenario: The live feed tells the page when something changed
    Given I am signed in
    And mission CODEC-1703 "Add from_cents" on codec-swarm-sandbox is waiting at its spec gate
    When I read the live feed since event 0 once
    Then it sends a change event with the latest event id

  Scenario: The harness page lists each role's model and capabilities
    Given I am signed in
    When I open "/harness"
    Then the page lists the specifier with model opus and floor sonnet
    And the page lists the backend-coder's MCP server context7

  Scenario: The orchestration page says whether Jev is on and shows the judge bands
    Given I am signed in
    When I open "/orchestration"
    Then the page shows the judge bands 0.95 and 0.80
    And the page shows the command-gate band from 0.87 to 0.93
