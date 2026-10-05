"""M3 comparison (option C): every ticket runs with Jev on and with Jev off, Codec standard pack, Auto mode.

Run:  uv run python spikes/m3_comparison.py [--workers 4] [--only CODEC-101,CODEC-201]
Each worker gets its own root (clone, worktrees, swarm.db) so parallel missions never share git or SQLite locks.
Logs go to spikes/out/m3/<ticket>.log. Each mission's PR is closed and its branch deleted once it finishes.
Then: uv run codec-swarm report --root ~/.codec-swarm-m3/w0 --root ~/.codec-swarm-m3/w1 ...
"""

from __future__ import annotations

import argparse
import queue
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

REPO = "https://github.com/malfarom29/codec-swarm-sandbox.git"
GH_REPO = "malfarom29/codec-swarm-sandbox"
ROOT = Path.home() / ".codec-swarm-m3"
LOGS = Path(__file__).parent / "out" / "m3"
TIMEOUT_S = 45 * 60


def jobs(series: str = "both", on_base: int = 101, off_base: int = 201) -> list[tuple[str, dict, bool]]:
    tickets = yaml.safe_load((Path(__file__).parent / "m3_tickets.yaml").read_text())
    out = []
    for i, ticket in enumerate(tickets):
        pair = [(f"CODEC-{on_base + i}", ticket, True), (f"CODEC-{off_base + i}", ticket, False)]
        pair = [j for j in pair if series == "both" or (series == "on") == j[2]]
        out += pair if i % 2 == 0 else pair[::-1]  # alternate which setting goes first
    return out


FREE: queue.Queue[int] = queue.Queue()  # worker roots not in use; a mission holds one for its whole run


def run(job: tuple[str, dict, bool]) -> tuple[str, int, str | None, float]:
    worker = FREE.get()
    try:
        return _run(job, worker)
    finally:
        FREE.put(worker)


def _run(job: tuple[str, dict, bool], worker: int) -> tuple[str, int, str | None, float]:
    ticket, spec, jev = job
    root = ROOT / f"w{worker}"
    LOGS.mkdir(parents=True, exist_ok=True)
    argv = [
        "uv", "run", "codec-swarm", "mission", "--repo-url", REPO, "--ticket", ticket,
        "--title", spec["title"], "--description", spec["description"],
        "--pack", "codec-standard", "--autonomy", "auto", "--yes", "--root", str(root),
    ] + ([] if jev else ["--no-jev"])
    started = time.monotonic()
    with (LOGS / f"{ticket}.log").open("w") as log:
        try:
            code = subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT, timeout=TIMEOUT_S, env={**_env(), "PYTHONUNBUFFERED": "1"}).returncode
        except subprocess.TimeoutExpired:
            code = -1
    text = (LOGS / f"{ticket}.log").read_text()
    match = re.search(r"https://github\.com/\S+/pull/\d+", text)
    pr = match.group(0) if match else None
    if pr:
        subprocess.run(["gh", "pr", "close", pr, "--repo", GH_REPO, "--delete-branch", "--comment", "M3 comparison run: metrics recorded, closing."], capture_output=True)
    return ticket, code, pr, (time.monotonic() - started) / 60


def _env() -> dict[str, str]:
    import os

    return {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--only", default="")
    parser.add_argument("--series", choices=["both", "on", "off"], default="both", help="run Jev on, off, or both")
    parser.add_argument("--on-base", type=int, default=101, help="first ticket number for Jev-on missions")
    parser.add_argument("--off-base", type=int, default=201, help="first ticket number for Jev-off missions")
    args = parser.parse_args()
    todo = jobs(args.series, args.on_base, args.off_base)
    if args.only:
        wanted = set(args.only.split(","))
        todo = [j for j in todo if j[0] in wanted]
    print(f"{len(todo)} missions on {args.workers} workers", flush=True)
    for worker in range(args.workers):
        FREE.put(worker)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run, job) for job in todo]
        for future in futures:
            ticket, code, pr, minutes = future.result()
            print(f"{ticket}: exit {code} · {minutes:.1f} min · {pr or 'no PR'}", flush=True)
    roots = " ".join(f"--root {ROOT / f'w{i}'}" for i in range(args.workers))
    print(f"\nreport: uv run codec-swarm report {roots}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
