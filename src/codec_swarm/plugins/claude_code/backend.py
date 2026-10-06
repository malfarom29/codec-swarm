"""AgentBackend that runs each step as a Claude Code turn through the Claude Agent SDK."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Callable
from typing import Any

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
    ToolUseBlock,
)

from codec_swarm.domain import Handoff, Mission
from codec_swarm.harness import SessionSpec, resolve_env_refs
from codec_swarm.plugins.api import AgentEvent, StepRequest
from codec_swarm.plugins.gate import Action, GateContext, hard_rules
from codec_swarm.store import SessionStore

# The step's final answer must match this schema, so the handoff never depends on parsing prose.
HANDOFF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "What you did and what is left, for the next role."},
        "send_back": {"type": "boolean", "description": "True to send the work back one step for a fix."},
        "commit_message": {
            "type": "string",
            "description": "Conventional Commits message for your code changes, or an empty string if you changed no files.",
        },
        "change_summary": {
            "type": "string",
            "description": (
                "For the pull request: what this lane's change does and why, as one plain description a reviewer reads. "
                "Describe the whole change so far, not what you or other roles did. Empty if no code has changed yet."
            ),
        },
        "files_touched": {"type": "array", "items": {"type": "string"}},
        "questions": {"type": "array", "items": {"type": "string"}, "description": "Questions for a human."},
    },
    "required": ["summary", "send_back", "commit_message", "change_summary", "files_touched", "questions"],
    "additionalProperties": False,
}
LANE_ORDER_FIELD: dict[str, Any] = {
    "type": "array",
    "description": "Every repo in the mission, with the repos whose lanes must reach the judge before it starts (an API before its clients).",
    "items": {
        "type": "object",
        "properties": {"repo": {"type": "string"}, "after": {"type": "array", "items": {"type": "string"}}},
        "required": ["repo", "after"],
        "additionalProperties": False,
    },
}


def handoff_schema(plans_lanes: bool) -> dict[str, Any]:
    if not plans_lanes:
        return HANDOFF_SCHEMA
    return {
        **HANDOFF_SCHEMA,
        "properties": {**HANDOFF_SCHEMA["properties"], "lane_order": LANE_ORDER_FIELD},
        "required": [*HANDOFF_SCHEMA["required"], "lane_order"],
    }


MAX_EVENT_TEXT = 2000
HANDOFF_RETRY_PROMPT = "You ended your step without your structured handoff. Reply now with only your structured handoff for this step."


class HandoffMissing(RuntimeError):
    """The step ended without a structured handoff."""


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_EVENT_TEXT:
        return value[:MAX_EVENT_TEXT] + f"… ({len(value) - MAX_EVENT_TEXT} more chars)"
    if isinstance(value, dict):
        return {k: _clip(v) for k, v in value.items()}
    return value


def task_prompt(request: StepRequest) -> str:
    lines = [f"You are the {request.role} for ticket {request.mission.ticket}: {request.mission.title or 'untitled'}."]
    if request.incoming:
        lines += [
            "",
            f"Incoming handoff from the {request.incoming.from_role}:",
            request.incoming.summary,
            *(f"- Question: {q}" for q in request.incoming.questions),
        ]
    if request.messages:
        lines += ["", "Messages from the human running this mission (they outrank the handoff where they disagree):", *(f"- {m}" for m in request.messages)]
    lines += ["", "Do your role's work in this worktree, then finish with your structured handoff."]
    return "\n".join(lines)


class ClaudeCodeBackend:
    def __init__(
        self,
        session_for: Callable[[Mission, str], SessionSpec],
        gate: Any,  # AllowlistGate or JevCommandGate; decide() may be sync or async
        sessions: SessionStore,
        env: dict[str, str] | None = None,
    ) -> None:
        self._session_for = session_for
        self._gate = gate
        self._sessions = sessions
        self._env = env

    def _options(self, spec: SessionSpec, mission: Mission, model: str, pending: list[AgentEvent]) -> ClaudeAgentOptions:
        ctx = GateContext(ticket=mission.ticket, repo=mission.repo, role=spec.role, worktree=spec.cwd, autonomy=mission.autonomy)

        async def can_use_tool(name: str, tool_input: dict[str, Any], _: ToolPermissionContext):
            decision = self._gate.decide(name, tool_input, ctx)
            if inspect.isawaitable(decision):
                decision = await decision
            pending.append(AgentEvent(kind="gate.decision", role=spec.role, payload=_clip({"tool": name, "input": tool_input, **decision.model_dump()})))
            if decision.action is Action.ALLOW:
                return PermissionResultAllow()
            # Approvals from inside a running step reach the Inbox in M4; until then the agent is told to ask in its handoff.
            return PermissionResultDeny(message=f"Blocked: {decision.reason}. Do not retry; list it in your handoff questions.")

        async def pre_tool_use(hook_input: dict[str, Any], tool_use_id: str | None, _: Any) -> dict[str, Any]:
            # Runs on every call, even ones a settings file pre-approves (M0), so the hard rules cannot be skipped.
            hard = hard_rules(hook_input.get("tool_name", ""), hook_input.get("tool_input", {}), ctx)
            if hard is None or hard.action is Action.ALLOW:
                return {}
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": hard.reason}}

        return ClaudeAgentOptions(
            cwd=str(spec.cwd),
            resume=self._sessions.get(mission.ticket, mission.repo, spec.role),
            model=model,
            system_prompt=spec.system_prompt,
            mcp_servers=resolve_env_refs(spec.mcp_servers, self._env),
            strict_mcp_config=True,  # only the harness's servers: never the machine's or the account's other MCP configs
            skills=list(spec.skills),
            allowed_tools=list(spec.allowed_tools),
            setting_sources=list(spec.setting_sources),
            can_use_tool=can_use_tool,
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[pre_tool_use])]},
            max_turns=spec.max_turns,
            output_format={"type": "json_schema", "schema": handoff_schema(spec.plans_lanes)},
        )

    async def run_step(self, request: StepRequest) -> AsyncIterator[AgentEvent]:
        spec = self._session_for(request.mission, request.role)
        pending: list[AgentEvent] = []
        result: ResultMessage | None = None
        model = spec.model if spec.model_forced else (request.model or spec.model)
        last_text = ""
        turns, cost = 0, 0.0
        async with ClaudeSDKClient(options=self._options(spec, request.mission, model, pending)) as client:
            for attempt, prompt in enumerate((task_prompt(request), HANDOFF_RETRY_PROMPT)):
                if attempt and isinstance(result.structured_output if result else None, dict):
                    break
                if attempt:
                    yield AgentEvent(kind="handoff.retry", role=spec.role, payload={"subtype": result.subtype if result else None})
                await client.query(prompt)
                async for message in client.receive_response():
                    while pending:
                        yield pending.pop(0)
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock) and block.text.strip():
                                last_text = block.text
                                yield AgentEvent(kind="agent.message", role=spec.role, payload={"text": _clip(block.text)})
                            elif isinstance(block, ToolUseBlock):
                                yield AgentEvent(kind="agent.tool", role=spec.role, payload=_clip({"tool": block.name, "input": block.input}))
                    elif isinstance(message, ResultMessage):
                        result = message
                        turns, cost = turns + message.num_turns, cost + (message.total_cost_usd or 0.0)
        while pending:
            yield pending.pop(0)

        if result is None:
            raise HandoffMissing(f"{spec.role} step ended without a result")
        self._sessions.record(request.mission.ticket, request.mission.repo, spec.role, result.session_id, model, turns, cost)
        yield AgentEvent(
            kind="cost",
            role=spec.role,
            payload={"session_id": result.session_id, "turns": turns, "cost_usd": cost, "usage": result.usage},
        )
        if isinstance(result.structured_output, dict):
            fields = {k: result.structured_output[k] for k in HANDOFF_SCHEMA["required"]}
            handoff = Handoff(from_role=spec.role, **fields, lane_order=tuple(result.structured_output.get("lane_order") or ()))
        else:
            # Still no handoff after the retry: hand the lane to a human instead of crashing the mission.
            handoff = Handoff(
                from_role=spec.role,
                summary=f"No structured handoff ({result.subtype}). Last message: {_clip(last_text)}",
                questions=("The agent ended its step without a structured handoff. Check its work before the lane continues.",),
                incomplete=True,
            )
        yield AgentEvent(kind="handoff", role=spec.role, payload=handoff.model_dump())
