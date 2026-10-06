<p align="center"><img src="docs/banner.png" alt="codec: every agent on the same frequency" width="100%"></p>

**codec-swarm** is a local, single-user orchestrator that takes a ticket to a reviewed change by running a swarm of Claude Code agents over your repos. Each agent plays one role: it writes the spec, builds with tests first, reviews, hardens or checks the work. They pass the work along through structured handoffs, each lane works in its own git worktree, and nothing reaches GitHub until you say so.

It adapts [Uncle Bob's SwarmForge](https://github.com/unclebob/swarm-forge) (roles, durable handoffs, worktrees). [LangGraph](https://langchain-ai.github.io/langgraph/) drives the flow, the [Claude Agent SDK](https://docs.claude.com/en/docs/agent-sdk/overview) runs the agents, and Jev (optional, via `typesafe-sdk`) routes models, scores commands and judges when a lane is done.

## How a mission works

```
ticket ─► Specifier ─► Architect ─► [spec gate] ─► one lane per repo:
          Gherkin      lane order     you approve    coder ─► reviewer ─► hardener ─► QA ─► judge ─► local PR
```

1. **Planning.** The Specifier writes Gherkin scenarios for each repo, and the Architect decides which repo's lane must come first. In the default *Gated* mode you approve the spec.
2. **Lanes.** Each repo gets a worktree on a feature branch. Its roles work in turn and can send the work back. Agents never commit: the orchestrator commits each step with the agent's message. When another lane depends on this one, the upstream branch is pushed so the dependent lane can code against it.
3. **Judge.** The repo's checks run (tests, lint, mutation, …). With Jev, every approved scenario is scored, and the lane's score is its weakest scenario. The score falls in one of three bands: *approve*, *review* (you look at it) or *stop* (it goes back to the coder).
4. **Local PR.** A judged lane is squashed into one commit with one plain description of the change. You review the diff in the dashboard, pick a target branch, then choose **Push and open on GitHub** or **Merge locally**.

Every step is recorded in an event log, and the graph is checkpointed after each step, so a crash or a restart picks up where it stopped.

### Safety rails

- Every tool call goes through a command gate:
  - These always ask you: `git push`, publishing, migrations, `kubectl` and `terraform apply`, inline code, and any path outside the lane's worktree.
  - Otherwise, a command runs only if it is in the repo's allowlist or scores high enough with Jev.
- The dashboard binds to `127.0.0.1` only and requires a per-launch token.
- Secrets live in `~/.codec-swarm/.env` (mode 600) or your environment, never in the database, the event log, handoffs or prompts.
- Nothing under `.swarm/` is ever committed. Specs and handoffs are kept in `~/.codec-swarm/missions/<ticket>/<repo>/`.
- Nothing is ever merged automatically.

## Requirements

- Python 3.13 and [uv](https://docs.astral.sh/uv/)
- [Claude Code](https://docs.claude.com/en/docs/claude-code) logged in (`claude`): the agents run on your account
- [GitHub CLI](https://cli.github.com/) logged in (`gh auth login`) to push PRs (not needed to merge locally)
- Optional: `TYPESAFE_API_KEY` to turn on Jev. Without it you get fixed models per role, the allowlist and a checks-only judge.
- Optional: a Jira Cloud API token, to pull tickets into the board

## Getting started

```bash
uv sync
cp .env.example .env            # optional: set TYPESAFE_API_KEY (or put it in ~/.codec-swarm/.env)
uv run codec-swarm up           # dashboard on http://127.0.0.1:8765
```

`up` prints a URL with a launch token; open that one, since the bare address answers 401. Use `--port` to change the port and `--root` to use a workspace other than `~/.codec-swarm`. Keep the terminal open while missions run.

### Your first mission

1. **Repos.** Paste the repo's URL and click Clone. If the repo has no `.swarm/config.yaml`, write a local config (at least `stack:`) on its page. It stays on your machine; see [Repo config](#repo-config).
2. **New mission.** Enter a ticket key, title, what needs to change (acceptance criteria, one per line) and one or more repo URLs. Each repo then gets a field for the branch its lane starts from; it defaults to the configured `branch_flow.base`, and you can pick any of origin's branches. Then choose an autonomy mode:
   - **Gated** (default): stops at the spec and PR gates.
   - **Auto:** stops only when the judge isn't sure.
   - **Manual:** stops after every step.
3. **Inbox.** Approve the spec, or send it back. A send-back can carry instructions, which reach the role that picks the work up as part of its incoming handoff.
4. **Follow the work.** The board shows missions *by stage* or *by agent*. A mission's page has tabs:
   - **Activity**
   - **Agents:** each role's output, a box to message that role at its next step, and `claude --resume` to attach to its session
   - **Definition of Done:** checks, scenario scores and the verdict
   - **Costs**
5. **Local PR.** Review the description and diff, set the target branch, then choose:
   - **Push and open on GitHub**
   - **Merge locally:** nothing is pushed; the page shows the `git push` command for when you're ready.

   You can also **Send back** the lane.

**Mission controls.** The mission page header has:
- **Resume:** continue from the last checkpoint, for example after an error or a server restart.
- **Restart:** set the mission's worktrees, local branches, local PRs and agent sessions aside, then run it again from the first role. Pushed branches are kept. Earlier runs' handoffs stay in `~/.codec-swarm/missions/<ticket>.runN/`.
- **Restart with changes:** the same, starting from the mission's form so you can edit it first.

**Update from base.** Each lane can be rebased onto the newest commit of its base branch:
- Clean rebase: the local PR is rewritten and the repo's checks run again.
- Conflict: nothing changes and the conflicting files are listed. If the lane is waiting at its PR or review gate, you can instead send the conflicts to the coder. The base branch is merged in with conflict markers left in place, and the coder is told to resolve them; the orchestrator commits the merge.

Neither works while a step is running.

**My requests** gives the same progress in plain steps, with any questions the agents asked you.

### From the terminal

```bash
uv run codec-swarm mission --ticket CODEC-700 --title "Add sum_amounts" \
  --repo-url git@github.com:you/payments.git        # repeat --repo-url for a multi-repo mission
uv run codec-swarm mission --ticket CODEC-700 --title "Add sum_amounts" \
  --repo-url git@github.com:you/payments.git --recover   # continue an interrupted mission
uv run codec-swarm report                           # cost, time and verdicts per mission
```

Add `--base release/1.2` to start every lane from another branch, or `--base api=release/1.2` for one repo. The CLI asks at each gate. `--yes` answers gates for you, but never approves a lane with failed checks.

## Repo config

A repo's settings are built from three layers, each overriding the one before:

1. the stack default shipped in the pack (`packs/codec-standard/stacks/<stack>.yaml`: `python`, `nestjs`, `react-vite`)
2. the repo's own `.swarm/config.yaml`, if it has one
3. your local file `~/.codec-swarm/repos.d/<repo>.yaml`, edited on the Repos page

```yaml
stack: python
sensitive: false          # payments or personal data: stricter bands, never the Solo pack
branch_flow:
  base: develop
  branch: "feature/{ticket}-{slug}"
checks:
  - id: unit
    run: uv run pytest -q --junitxml=reports/junit.xml
    report: { kind: junit, path: reports/junit.xml }
allowlist:
  - uv run pytest
```

Business rules every agent should know go in `~/.codec-swarm/repos.d/<repo>.domain.md`, or in the repo's `.swarm/domain.md`.

## Environments

Each repo can have its own environment, managed in the **Environment** section of its page on the Repos screen:
- set a variable, paste a whole `.env` to import it, or delete one;
- once saved, values are shown masked (`••••` plus the last 4 characters) and are never shown back.

They're stored in `~/.codec-swarm/env/<repo>.env` (mode 600), never in the database, events, handoffs or prompts.

Every lane gets them in two ways:
- **as a file** in its worktree: `.env` by default, or a name you set per repo, such as `.env.local`. Git ignores it, so it's never committed. If the repo tracks a file with that name, it's left alone and only the second way is used.
- **as environment variables** for the repo's checks and the agents' sessions.

Agents can use the values but not print them. Reading an env file (`.env.example` and other templates excepted), dumping the environment (`printenv`, `env` and the like) or expanding a managed variable (`$DATABASE_URL`) always asks you.

On **New mission**, each repo has an optional *Environment for this mission* box. `KEY=value` lines there replace single values for that run only. The mission records only the variable names; the values are kept in `~/.codec-swarm/env/missions/<ticket>/`.

For a repo marked `sensitive: true`, values that look like live credentials are flagged: live Stripe keys, AWS access keys, Slack or GitHub tokens, and production hosts.

## Dashboard settings

- **Harness.** For each role you can override the model (never below the role's floor) and add MCP servers or skills; nothing can be removed. To make a new MCP server available, add it to `mcp_catalog` in the pack and put its token in `.env` as `${env:NAME}`.
- **Orchestration.** Turn Jev on or off, and tune the command-gate threshold and margin.
- **Jira.** Until you connect Jira, the Intake column shows example tickets. To connect, enter your site, email and API token: the token is checked against Jira, then saved only to `~/.codec-swarm/.env`. Tickets matching the JQL sync every 5 minutes. The default JQL is your tasks and subtasks in To Do or In Progress.

## Packs and roles

Roles, models and constitution layers live in `packs/`:

- `codec-standard`: Specifier, Architect, Backend coder, Reviewer, Hardener and QA.
- `solo`: a single Coder, for small, low-risk tickets.

Each agent's system prompt is built from these layers:

1. the core constitution
2. the repo's domain notes
3. the stack's rules
4. the role's own prompt
5. the mission
6. the commands this repo allows

## Development

```bash
uv run pytest            # offline: BDD scenarios per milestone, fake agents and judge
uv run pytest -m live    # also calls Claude Code (Haiku) and Jev for real
```

Scenarios live in `features/` and their steps in `tests/`. The spikes behind the M0 decisions are in `spikes/` and write their reports to `spikes/out/`.

```
src/codec_swarm/
  domain/      missions, handoffs, verdicts, bands
  graph/       LangGraph mission graph, runner, multi-lane coordinator
  harness/     packs, repo config, session resolution
  plugins/     Claude Code backend, command gate, judges, Jev, Jira
  store/       event log, sessions, settings, chat, views
  workspace/   clones, worktrees, local PRs, repos, secrets
  web/         FastAPI + Jinja + htmx dashboard
```
