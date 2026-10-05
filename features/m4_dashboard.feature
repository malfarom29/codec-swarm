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

  Scenario: A harness override changes a role's model and adds an MCP server
    Given I am signed in
    When I set the backend-coder of codec-standard to model opus with MCP server playwright
    Then the harness page shows backend-coder overridden to opus with +playwright
    And a backend-coder session resolves to model opus with servers context7 and playwright

  Scenario: The harness refuses a model below the role's floor
    Given I am signed in
    When I set the specifier of codec-standard to model haiku with no MCP server
    Then the harness page says "specifier cannot run below sonnet"
    And the specifier of codec-standard has no override

  Scenario: Orchestration settings change the command-gate band and can turn Jev off
    Given I am signed in
    When I save orchestration with Jev off, threshold 0.92 and margin 0.02
    Then the page shows the command-gate band from 0.90 to 0.94
    And the orchestration settings say Jev is off

  Scenario: A message to an agent joins its next step's prompt
    Given I am signed in
    And mission CODEC-1710 "Add from_cents" on codec-swarm-sandbox is waiting at its spec gate
    When I send "Use Decimal, not float" to the backend-coder of lane codec-swarm-sandbox in CODEC-1710
    Then the mission page of CODEC-1710 shows that message as queued
    When I approve the spec gate of CODEC-1710 from the inbox
    Then the backend-coder's step received "Use Decimal, not float"
    And the mission page of CODEC-1710 shows that message as delivered

  Scenario: A role that ran shows the command to attach to its session
    Given I am signed in
    And mission CODEC-1711 "Add from_cents" on codec-swarm-sandbox is waiting at its pr gate
    And the backend-coder of lane codec-swarm-sandbox in CODEC-1711 ran as session "sess-123"
    When I open the mission page of CODEC-1711
    Then it shows "claude --resume sess-123" for the backend-coder

  Scenario: Before Jira is connected the Intake column shows example tickets
    Given I am signed in
    When I open "/"
    Then the Intake column shows CODEC-901 marked as an example
    And the Jira bar offers to connect Jira

  Scenario: Connecting Jira keeps the token out of the database and fetches tickets
    Given I am signed in
    And Jira at "https://team.atlassian.net" answers for "me@example.com" with ticket PAY-7 "Refund partial captures"
    When I connect Jira with site "team.atlassian.net", email "me@example.com" and a token
    Then the Intake column shows PAY-7 and no example tickets
    And the token is in the root's .env, readable only by me, and nowhere in the database
    And the Start mission link for PAY-7 fills in its title

  Scenario: A rejected Jira token saves nothing
    Given I am signed in
    And Jira at "https://team.atlassian.net" rejects every token
    When I connect Jira with site "team.atlassian.net", email "me@example.com" and a token
    Then the Jira page says "rejected the email or API token"
    And Jira is not connected

  Scenario: My requests shows plain progress and what waits on me
    Given I am signed in
    And mission CODEC-1712 "Add from_cents" on codec-swarm-sandbox is waiting at its spec gate
    When I open "/requests"
    Then CODEC-1712 is at the step "Your OK on the spec"
    And it says I need to approve the spec
