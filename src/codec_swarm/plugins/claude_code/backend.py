"""AgentBackend that runs each step as a Claude Code turn through the Claude Agent SDK."""

from __future__ import annotations

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
from codec_swarm.plugins.gate import Action, AllowlistGate, GateContext, hard_rules
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
        "files_touched": {"type": "array", "items": {"type": "string"}},
        "questions": {"type": "array", "items": {"type": "string"}, "description": "Questions for a human."},
    },
    "required": ["summary", "send_back", "commit_message", "files_touched", "questions"],
    "additionalProperties": False,
}
MAX_EVENT_TEXT = 2000


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
    lines += ["", "Do your role's work in this worktree, then finish with your structured handoff."]
    return "\n".join(lines)


class ClaudeCodeBackend:
    def __init__(
        self,
        session_for: Callable[[Mission, str], SessionSpec],
        gate: AllowlistGate,
        sessions: SessionStore,
        env: dict[str, str] | None = None,
    ) -> None:
        self._session_for = session_for
        self._gate = gate
        self._sessions = sessions
        self._env = env

    def _options(self, spec: SessionSpec, mission: Mission, pending: list[AgentEvent]) -> ClaudeAgentOptions:
        ctx = GateContext(ticket=mission.ticket, role=spec.role, worktree=spec.cwd, autonomy=mission.autonomy)

        async def can_use_tool(name: str, tool_input: dict[str, Any], _: ToolPermissionContext):
            decision = self._gate.decide(name, tool_input, ctx)
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
            model=spec.model,
            system_prompt=spec.system_prompt,
            mcp_servers=resolve_env_refs(spec.mcp_servers, self._env),
            skills=list(spec.skills),
            allowed_tools=list(spec.allowed_tools),
            setting_sources=list(spec.setting_sources),
            can_use_tool=can_use_tool,
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[pre_tool_use])]},
            max_turns=spec.max_turns,
            output_format={"type": "json_schema", "schema": HANDOFF_SCHEMA},
        )

    async def run_step(self, request: StepRequest) -> AsyncIterator[AgentEvent]:
        spec = self._session_for(request.mission, request.role)
        pending: list[AgentEvent] = []
        result: ResultMessage | None = None
        async with ClaudeSDKClient(options=self._options(spec, request.mission, pending)) as client:
            await client.query(task_prompt(request))
            async for message in client.receive_response():
                while pending:
                    yield pending.pop(0)
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            yield AgentEvent(kind="agent.message", role=spec.role, payload={"text": _clip(block.text)})
                        elif isinstance(block, ToolUseBlock):
                            yield AgentEvent(kind="agent.tool", role=spec.role, payload=_clip({"tool": block.name, "input": block.input}))
                elif isinstance(message, ResultMessage):
                    result = message
        while pending:
            yield pending.pop(0)

        if result is None:
            raise HandoffMissing(f"{spec.role} step ended without a result")
        self._sessions.record(
            request.mission.ticket, request.mission.repo, spec.role, result.session_id, spec.model,
            result.num_turns, result.total_cost_usd or 0.0,
        )
        yield AgentEvent(
            kind="cost",
            role=spec.role,
            payload={"session_id": result.session_id, "turns": result.num_turns, "cost_usd": result.total_cost_usd, "usage": result.usage},
        )
        if not isinstance(result.structured_output, dict):
            raise HandoffMissing(f"{spec.role} ended without a structured handoff ({result.subtype})")
        handoff = Handoff(from_role=spec.role, **{k: result.structured_output[k] for k in HANDOFF_SCHEMA["required"]})
        yield AgentEvent(kind="handoff", role=spec.role, payload=handoff.model_dump())
