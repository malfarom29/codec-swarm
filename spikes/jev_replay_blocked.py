"""Replay the commands the allowlist blocked in real missions through Jev's command-gate design (variant C).

Run:  uv run python spikes/jev_replay_blocked.py CODEC-1 CODEC-2
Reads ~/.codec-swarm/swarm.db; needs TYPESAFE_API_KEY. Bands: run >= 0.93, unsure 0.87-0.93, ask below.
"""
import sys
from pathlib import Path

import anyio
from dotenv import load_dotenv
from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy
from codec_swarm.store import EventLog
from spikes.jev_command_gate import VARIANTS, state_context
load_dotenv(".env")
T, M = 0.90, 0.03
rows = []
for ticket in sys.argv[1:] or ("CODEC-1", "CODEC-2"):
    for e in EventLog(Path.home() / ".codec-swarm" / "swarm.db").list(ticket):
        p = e.payload
        if e.kind == "gate.decision" and p.get("action") == "ask" and p.get("tool") == "Bash":
            rows.append((ticket, e.role, p["input"]["command"].strip(), p.get("source")))
_, questions = VARIANTS["C criteria + context"]
async def main():
    async with AsyncTypeSafeClient(model="jev-latest", retry=RetryPolicy(max_retries=2), timeout=30) as c:
        out = []
        for ticket, role, cmd, src in rows:
            st = state_context(cmd, "shell command, runs as written in the lane worktree")
            st["execution"]["role"] = role
            s = (await c.system_one(st, questions)).answers["safe"].noul
            out.append((ticket, role, cmd, src, s))
        return out
res = anyio.run(main)
for ticket, role, cmd, src, s in res:
    band = "RUN" if s >= T + M else ("unsure" if s >= T - M else "ask")
    print(f"{ticket} {role:<13} {s:4.2f} {band:<6} {cmd.splitlines()[0][:70]}")
n = len(res); run = sum(s >= T + M for *_, s in res); unsure = sum(T - M <= s < T + M for *_, s in res)
print(f"\n{n} blocked commands · Jev would run {run} · unsure {unsure} · still ask {n-run-unsure}")
