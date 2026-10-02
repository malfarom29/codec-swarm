# codec-swarm

Local, single-user orchestrator that runs a swarm of Claude Code agents from a Jira ticket to a reviewed PR.
Plan: [codec-swarm — Architecture & Plan](https://claude.ai/code/artifact/5280a148-35fd-48f5-b40c-07f3ec2c00f1).

## Setup

```bash
uv sync
cp .env.example .env   # then set TYPESAFE_API_KEY
```

## Tests

```bash
uv run pytest            # offline tests only
uv run pytest -m live    # also calls Claude Code (Haiku) and Jev for real
```

## M0 spike

`features/m0_spike.feature` is the M0 gate. The spikes behind it also run on their own and write reports to `spikes/out/`:

```bash
uv run python spikes/m0_agent_sdk.py   # Agent SDK options, permission bridge, CLAUDE.md, resume
uv run python spikes/m0_jev.py --repeat 3   # Jev typed questions, latency, tokens
uv run python spikes/jev_command_gate.py   # compare ways of asking Jev "is this command safe?"
uv run python spikes/jev_command_gate.py --noise 15   # measure score spread to size the gate margin
```
