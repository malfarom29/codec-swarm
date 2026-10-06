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

  Scenario: Adding a repo from the Repos page clones it and offers a starter local config
    Given I am signed in
    And a local origin repo "billing" with no swarm config
    When I add that repo on the Repos page
    Then the Repos page lists billing as needing config
    And the repo page for billing offers a starter local config with stack python

  Scenario: A local config configures a repo without committing anything to it
    Given I am signed in
    And a local origin repo "billing" with no swarm config
    And I added that repo on the Repos page
    When I save billing's local config with stack python, allowlist "uv run pytest" and domain "Amounts are integer cents."
    Then the repo page for billing says stack and allowlist come from local
    And billing's config in effect allows "uv run pytest" and has the domain "Amounts are integer cents."
    And the billing clone has no .swarm files

  Scenario: A local config that does not load is refused
    Given I am signed in
    And a local origin repo "billing" with no swarm config
    And I added that repo on the Repos page
    When I save billing's local config as "stack: cobol"
    Then the save is refused with "Unknown stack 'cobol'"
    And billing has no local config file

  Scenario: The local PR page shows the description and diff, and merges into the branch I pick
    Given I am signed in
    And mission CODEC-1720 "Partial refunds" has a local PR for codec-payment
    When I open the local PR of CODEC-1720 for codec-payment
    Then it shows the description "Refunds can now be partial." and the diff of src/refunds.ts
    And the target branch defaults to develop
    When I merge it locally into develop from the page
    Then the local PR says it was merged into develop
    And the mission page of CODEC-1720 shows the lane merged into develop

  Scenario: New mission starts each lane from the configured base unless I pick another
    Given I am signed in
    And a local origin repo "billing" with no swarm config
    And origin "billing" also has a branch release/2026.10
    And I added that repo on the Repos page
    And I saved billing's local config with stack python and base develop
    When I list the bases for billing on the New mission form
    Then billing's base defaults to develop and offers release/2026.10
    When I start mission CODEC-1730 on billing from release/2026.10
    Then mission CODEC-1730 starts billing from release/2026.10

  Scenario: A base that origin doesn't have is refused before the mission starts
    Given I am signed in
    And a local origin repo "billing" with no swarm config
    And I added that repo on the Repos page
    And I saved billing's local config with stack python and base develop
    When I start mission CODEC-1731 on billing from no-such-branch
    Then the form says "billing has no branch 'no-such-branch' on origin"
    And no mission CODEC-1731 was started

  Scenario: The live feed asks for a sound when a gate opens and when a PR is ready
    Given I am signed in
    And mission CODEC-1740 "Add from_cents" on codec-swarm-sandbox is waiting at its pr gate
    When I read the live feed since event 0 once
    Then it sends an input notification for the spec gate of CODEC-1740
    And it sends an input notification for the pr gate of CODEC-1740

  Scenario: A local config with a misplaced key is refused
    Given I am signed in
    And a local origin repo "billing" with no swarm config
    And I added that repo on the Repos page
    When I save billing's local config with the allowlist indented under branch_flow
    Then the save is refused with "Extra inputs are not permitted"
    And billing has no local config file

  Scenario: Sending back with instructions hands them to the role that picks the work up
    Given I am signed in
    And mission CODEC-1750 "Add from_cents" on codec-swarm-sandbox is waiting at its spec gate
    When I send back the spec gate of CODEC-1750 with "Cover negative amounts too"
    Then the specifier's next step got a handoff from the human with "Cover negative amounts too"
    And the activity of CODEC-1750 shows the instructions

  Scenario: A reconnecting live feed resumes after the last event it saw
    Given I am signed in
    And mission CODEC-1760 "Add from_cents" on codec-swarm-sandbox is waiting at its spec gate
    When I reconnect to the live feed having seen every event
    Then it sends nothing

  Scenario: A repo's environment is managed from its page and never shown back
    Given I am signed in
    And a local origin repo "billing" with no swarm config
    And I added that repo on the Repos page
    And I saved billing's local config with stack python and base develop
    When I save billing's variable DATABASE_URL as "postgres://app:s3cret-pass@localhost:5432/billing"
    And I paste a .env into billing with REDIS_URL "redis://localhost:6379"
    Then billing's environment lists DATABASE_URL and REDIS_URL masked
    And the value "s3cret-pass" appears nowhere on the page or in the database

  Scenario: A sensitive repo flags values that look like live credentials
    Given I am signed in
    And a local origin repo "billing" with no swarm config
    And I added that repo on the Repos page
    And billing's local config marks it sensitive
    When I save billing's variable STRIPE_KEY as "sk_live_51HabcdefghijKLMNOP"
    Then billing's STRIPE_KEY is flagged as a live Stripe key

  Scenario: A mission can override one value without storing it in the mission
    Given I am signed in
    And a local origin repo "billing" with no swarm config
    And I added that repo on the Repos page
    And I saved billing's local config with stack python and base develop
    And I save billing's variable DATABASE_URL as "postgres://localhost/billing"
    When I start mission CODEC-1770 on billing overriding DATABASE_URL with "postgres://localhost/billing_test"
    Then billing's lane in CODEC-1770 gets DATABASE_URL "postgres://localhost/billing_test"
    And the mission's stored request names DATABASE_URL but not its value

  Scenario: Restarting a mission runs it again from the first role
    Given I am signed in
    And mission CODEC-1780 "Add from_cents" on codec-swarm-sandbox is waiting at its pr gate
    When I restart CODEC-1780 from its page
    Then CODEC-1780 is on its second run, waiting at the spec gate
    And the specifier ran again

  Scenario: Restarting with changes runs the edited request
    Given I am signed in
    And mission CODEC-1781 "Add from_cents" on codec-swarm-sandbox is waiting at its spec gate
    When I open the restart form of CODEC-1781
    Then it is filled with the title "Add from_cents"
    When I restart CODEC-1781 with the title "Add from_cents and to_cents"
    Then mission CODEC-1781 now has the title "Add from_cents and to_cents"

  Scenario: A mission that is working can't be restarted
    Given I am signed in
    And mission CODEC-1782 "Add from_cents" on codec-swarm-sandbox is waiting at its spec gate
    And CODEC-1782 is in the middle of a step
    When I restart CODEC-1782 from its page
    Then the mission page says "is working right now"

  Scenario: Updating a lane from its base rebases the local PR and reruns the checks
    Given I am signed in
    And mission CODEC-1783 "Partial refunds" has a local PR for codec-payment
    And codec-payment's checks are "test -f README.md"
    And origin's develop gets a new commit adding docs/CHANGELOG.md
    When I update codec-payment of CODEC-1783 from its base
    Then the local PR of CODEC-1783 for codec-payment sits on the new develop with one commit
    And the activity of CODEC-1783 says the checks pass

  Scenario: An update that conflicts changes nothing
    Given I am signed in
    And mission CODEC-1784 "Partial refunds" has a local PR for codec-payment
    And codec-payment's checks are "test -f README.md"
    And origin's develop gets a conflicting src/refunds.ts
    When I update codec-payment of CODEC-1784 from its base
    Then the activity of CODEC-1784 says it conflicts in src/refunds.ts
    And the local PR of CODEC-1784 for codec-payment is unchanged

  Scenario: Restarting one lane runs it again with my instructions and keeps the spec
    Given I am signed in
    And mission CODEC-1790 "Add from_cents" on codec-swarm-sandbox is waiting at its pr gate
    When I restart lane codec-swarm-sandbox of CODEC-1790 with "Use the existing Money helpers"
    Then the backend-coder's first step got "Use the existing Money helpers"
    And the specifier did not run again

  Scenario: With a worker, the dashboard queues the mission and the worker runs it
    Given the dashboard sends missions to a worker queue
    And I am signed in
    When I start mission CODEC-1800 "Add from_cents" on codec-swarm-sandbox in gated mode
    Then the start of CODEC-1800 is queued and nothing has run yet
    And the mission page of CODEC-1800 shows it as working
    When the worker runs what is queued
    Then the inbox shows the spec gate of CODEC-1800

  Scenario: A worker that died mid-command is picked up by the next one
    Given the dashboard sends missions to a worker queue
    And I am signed in
    And mission CODEC-1801 "Add from_cents" on codec-swarm-sandbox is waiting at its spec gate
    And a worker died while approving the spec of CODEC-1801
    When the worker runs what is queued
    Then CODEC-1801 was resumed from its checkpoint, with my approval applied
