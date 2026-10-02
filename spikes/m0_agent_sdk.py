"""M0 spike: check that the Claude Agent SDK behaves the way the codec-swarm plan assumes.

Run:  uv run python spikes/m0_agent_sdk.py
Uses your Claude Code login (Haiku, a few turns). Writes spikes/out/m0_agent_sdk.json.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolPermissionContext,
)

# Fields the harness sets on every session (plan: "Claude Code backend", step 2).
PLAN_FIELDS = [
    "cwd",
    "resume",
    "model",
    "system_prompt",
    "mcp_servers",
    "allowed_tools",
    "setting_sources",
    "can_use_tool",
    "max_turns",
]
CODE_WORD = "PELICAN-42"
MARKER = "SWORDFISH"
OUT = Path(__file__).parent / "out"


def check_fields() -> dict[str, Any]:
    have = {f.name for f in dataclasses.fields(ClaudeAgentOptions)}
    useful = {"session_id", "fork_session", "session_store", "plugins", "skills", "max_budget_usd", "sandbox"}
    return {"missing": [f for f in PLAN_FIELDS if f not in have], "also_available": sorted(have & useful)}


def make_repo() -> Path:
    """A throwaway lane worktree whose CLAUDE.md asks for a marker word."""
    root = Path(tempfile.mkdtemp(prefix="codec-m0-")).resolve()
    (root / "CLAUDE.md").write_text(f"End every reply with the word {MARKER}.\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


def _inside(repo: Path, tool_input: dict[str, Any]) -> bool:
    raw = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("notebook_path")
    if not raw:
        return True
    path = Path(raw)
    if not path.is_absolute():
        path = repo / path
    return path.resolve().is_relative_to(repo)


def build_options(repo: Path, log: list[dict[str, Any]], resume: str | None = None) -> ClaudeAgentOptions:
    async def can_use_tool(name: str, tool_input: dict[str, Any], ctx: ToolPermissionContext):
        allowed = _inside(repo, tool_input)
        log.append({"tool": name, "input": tool_input, "allowed": allowed})
        if allowed:
            return PermissionResultAllow()
        return PermissionResultDeny(message="Outside the lane worktree.")

    return ClaudeAgentOptions(
        cwd=str(repo),
        resume=resume,
        model="haiku",
        system_prompt="You are a test agent for the codec-swarm M0 spike. Be brief.",
        mcp_servers={},
        allowed_tools=["Read"],
        setting_sources=["project"],
        can_use_tool=can_use_tool,
        max_turns=4,
    )


async def run_turn(options: ClaudeAgentOptions, prompt: str) -> tuple[str, ResultMessage | None]:
    texts: list[str] = []
    result: ResultMessage | None = None
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                texts += [b.text for b in msg.content if isinstance(b, TextBlock)]
            elif isinstance(msg, ResultMessage):
                result = msg
    return "\n".join(texts), result


def _result_summary(result: ResultMessage | None) -> dict[str, Any]:
    if result is None:
        return {}
    return {
        "session_id": result.session_id,
        "num_turns": result.num_turns,
        "total_cost_usd": result.total_cost_usd,
        "usage": result.usage,
        "duration_ms": result.duration_ms,
        "is_error": result.is_error,
    }


async def first_turn(repo: Path) -> dict[str, Any]:
    log: list[dict[str, Any]] = []
    prompt = f"Remember this code word: {CODE_WORD}. Then create a file named note.txt containing exactly the word hello."
    reply, result = await run_turn(build_options(repo, log), prompt)
    note = repo / "note.txt"
    return {
        "repo": str(repo),
        "reply": reply,
        "result": _result_summary(result),
        "permission_log": log,
        "note_written": note.exists() and note.read_text().strip() == "hello",
        "follows_claude_md": MARKER in reply.upper(),
    }


async def resume_turn(repo: Path, session_id: str) -> dict[str, Any]:
    log: list[dict[str, Any]] = []
    prompt = "What code word did I give you earlier? Reply with only the code word."
    reply, result = await run_turn(build_options(repo, log, resume=session_id), prompt)
    return {"reply": reply, "result": _result_summary(result), "knows_code_word": CODE_WORD in reply.upper()}


async def settings_shadow_probe() -> dict[str, Any]:
    """Does an allow rule in the repo's own .claude/settings.json skip can_use_tool? Does a PreToolUse hook still run?"""
    repo = make_repo()
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Write"]}}))
    log: list[dict[str, Any]] = []
    hook_calls: list[str] = []

    async def pre_tool_use(hook_input, tool_use_id, context):
        hook_calls.append(hook_input.get("tool_name", "?"))
        return {}

    options = build_options(repo, log)
    options.hooks = {"PreToolUse": [HookMatcher(matcher=None, hooks=[pre_tool_use])]}
    await run_turn(options, "Create a file named probe.txt containing exactly the word probe.")
    return {
        "file_written": (repo / "probe.txt").exists(),
        "can_use_tool_called": bool(log),
        "pre_tool_use_hook_called": "Write" in hook_calls,
    }


def resume_in_new_process(repo: Path, session_id: str) -> dict[str, Any]:
    """Resume from a separate Python process, as the orchestrator would after a restart."""
    proc = subprocess.run(
        [sys.executable, __file__, "--resume", session_id, "--repo", str(repo)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume")
    parser.add_argument("--repo")
    args = parser.parse_args()
    if args.resume:
        print(json.dumps(anyio.run(resume_turn, Path(args.repo), args.resume)))
        return 0

    report: dict[str, Any] = {"fields": check_fields()}
    repo = make_repo()
    report["first_turn"] = anyio.run(first_turn, repo)
    session_id = report["first_turn"]["result"].get("session_id")
    if session_id:
        report["resume"] = resume_in_new_process(repo, session_id)
        report["attach_command"] = f"cd {repo} && claude --resume {session_id}"
    report["settings_shadow"] = anyio.run(settings_shadow_probe)

    OUT.mkdir(exist_ok=True)
    (OUT / "m0_agent_sdk.json").write_text(json.dumps(report, indent=2, default=str))

    checks = {
        "fields present": not report["fields"]["missing"],
        "note.txt written in repo": report["first_turn"]["note_written"],
        "permission requests bridged": bool(report["first_turn"]["permission_log"]),
        "CLAUDE.md followed": report["first_turn"]["follows_claude_md"],
        "session resumed in new process": report.get("resume", {}).get("knows_code_word", False),
    }
    for name, ok in checks.items():
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    shadow = report["settings_shadow"]
    print(
        "INFO  repo settings allow Write: "
        f"can_use_tool called={shadow['can_use_tool_called']}, PreToolUse hook called={shadow['pre_tool_use_hook_called']}"
    )
    print(f"cost: ${report['first_turn']['result'].get('total_cost_usd')}  report: {OUT / 'm0_agent_sdk.json'}")
    if "attach_command" in report:
        print(f"attach by hand: {report['attach_command']}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
