"""M0 spike: check that Jev answers codec-swarm's typed questions through typesafe-sdk.

Run:  uv run python spikes/m0_jev.py [--repeat N]
Needs TYPESAFE_API_KEY (environment or .env). Writes spikes/out/m0_jev.json.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

import anyio
from dotenv import load_dotenv
from typesafe_sdk import (
    AsyncTypeSafeClient,
    Choice,
    ChoiceAnswer,
    Noul,
    NoulAnswer,
    RetryPolicy,
    Score,
    ScoreAnswer,
)

MODEL = "jev-latest"  # jev-1.13 is not served; only jev-latest and jev-preview are listed
OUT = Path(__file__).parent / "out"

# A state summary shaped like the one the orchestrator will send (plan: "Security", item 7).
STATE = {
    "mission": {"ticket": "CODEC-1423", "title": "Partial refunds from the payment link", "autonomy": "gated"},
    "lane": {"repo": "codec-payment", "stage": "review"},
    "handoff": {
        "from": "reviewer",
        "summary": "Approved the idempotency fix on POST /refunds. Mutation tests have not run yet.",
        "files_touched": 3,
    },
    "checks": {"unit": "142/142", "lint": "pass"},
    "pending_command": "npm run test:unit",
}

QUESTIONS = {
    "next_role": Choice(
        instructions="Which role should take this lane next?",
        criteria={
            "hardener": "Runs mutation tests and edge cases.",
            "qa": "Runs the e2e BDD suite.",
            "backend-coder": "Fixes the code.",
        },
    ),
    "command_safe": Noul(instructions="Is the pending command safe to run without asking a human?"),
    "readiness": Score(
        instructions="How ready is this lane for the judge?",
        criteria=["not ready", "needs work", "almost ready", "ready"],
    ),
}
EXPECTED = {"next_role": ChoiceAnswer, "command_safe": NoulAnswer, "readiness": ScoreAnswer}


def has_key() -> bool:
    load_dotenv()
    return bool(os.environ.get("TYPESAFE_API_KEY"))


def check_answers(answers: dict[str, Any]) -> dict[str, bool]:
    ok: dict[str, bool] = {}
    for name, kind in EXPECTED.items():
        a = answers.get(name)
        if not isinstance(a, kind):
            ok[name] = False
        elif isinstance(a, NoulAnswer):
            ok[name] = 0.0 <= a.noul <= 1.0
        else:
            ok[name] = abs(sum(a.probabilities.values()) - 1.0) < 0.02
    return ok


async def ask(repeat: int = 1) -> dict[str, Any]:
    async with AsyncTypeSafeClient(model=MODEL, retry=RetryPolicy(max_retries=2), timeout=30) as client:
        models = await client.models.list()
        names = [m.name for m in models.models]
        calls = []
        for _ in range(repeat):
            t0 = time.perf_counter()
            response = await client.system_one(STATE, QUESTIONS)
            calls.append({"latency_ms": round((time.perf_counter() - t0) * 1000), "response": response})
    last = calls[-1]["response"]
    return {
        "models": names,
        "pinned_model_available": MODEL in names,
        "model_used": last.model,
        "latency_ms": [c["latency_ms"] for c in calls],
        "usage": [c["response"].usage.model_dump() for c in calls],
        "answers": {k: v.model_dump() for k, v in last.answers.items()},
        "answers_valid": check_answers(last.answers),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()
    if not has_key():
        print("FAIL  TYPESAFE_API_KEY is not set (add it to .env)")
        return 1

    report = anyio.run(ask, args.repeat)
    OUT.mkdir(exist_ok=True)
    (OUT / "m0_jev.json").write_text(json.dumps(report, indent=2, default=str))

    checks = {f"{MODEL} available": report["pinned_model_available"]}
    checks |= {f"{name} answer valid": ok for name, ok in report["answers_valid"].items()}
    for name, ok in checks.items():
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    print(f"model: {report['model_used']}  latency ms: {report['latency_ms']}  usage: {report['usage']}")
    print(f"answers: {json.dumps(report['answers'])}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
