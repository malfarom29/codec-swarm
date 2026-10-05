"""The mission graph: every role is a node, every gate is an interrupt.

M1 runs one lane; multi-lane missions with dependency order come in M3.
"""

from __future__ import annotations

import inspect
import operator
from typing import Annotated, Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from codec_swarm.domain import Autonomy, Band, GateKind, Handoff, JudgeBands, Mission, Pack
from codec_swarm.plugins.api import AgentBackend, EventSink, HandoffRecorder, Judge, Publisher, Router, StepRequest

APPROVE = "approve"
SEND_BACK = "send_back"


class MissionState(TypedDict, total=False):
    mission: dict[str, Any]
    trail: Annotated[list[str], operator.add]  # every node that did work, in order
    handoffs: Annotated[list[dict[str, Any]], operator.add]
    next: str  # where the last node routed to
    verdict: dict[str, Any] | None
    reworks: int  # judge send-backs so far
    status: str
    pr_url: str | None


def _mission(state: MissionState) -> Mission:
    return Mission.model_validate(state["mission"])


async def _resolved(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def build_graph(
    pack: Pack,
    backend: AgentBackend,
    router: Router,
    judge: Judge,
    events: EventSink,
    recorder: HandoffRecorder | None = None,
    publisher: Publisher | None = None,
) -> StateGraph:
    graph = StateGraph(MissionState)

    def role_node(role: str):
        async def run(state: MissionState) -> MissionState:
            mission = _mission(state)
            incoming = Handoff.model_validate(state["handoffs"][-1]) if state.get("handoffs") else None
            pick = await _resolved(router.pick_model(pack, mission, role, incoming))
            if pick.next:
                events.append(mission.ticket, "decision", {"slot": "model", **pick.model_dump()}, role=role)
            handoff: Handoff | None = None
            request = StepRequest(mission=mission, role=role, incoming=incoming, model=pick.next or None)
            async for event in backend.run_step(request):
                events.append(mission.ticket, event.kind, event.payload, role=role)
                if event.kind == "handoff":
                    handoff = Handoff.model_validate(event.payload)
            if handoff is None:
                raise RuntimeError(f"{role} ended its step without a handoff")
            decision = await _resolved(router.next_role(pack, role, handoff))
            events.append(mission.ticket, "decision", {"slot": "next_role", **decision.model_dump()}, role=role)
            if recorder is not None:
                step = len(state.get("handoffs", [])) + 1
                sha = recorder.record(mission, step, handoff, decision.next)
                handoff = handoff.model_copy(update={"commit_sha": sha})
            return {"trail": [role], "handoffs": [handoff.model_dump()], "next": decision.next}

        return run

    async def judge_node(state: MissionState) -> MissionState:
        mission = _mission(state)
        bands = mission.bands or JudgeBands()
        verdict = await judge.evaluate(mission, [Handoff.model_validate(h) for h in state["handoffs"]])
        band = bands.classify(verdict)
        reworks = state.get("reworks", 0)
        if band is Band.APPROVE:
            nxt = "done" if mission.autonomy is Autonomy.AUTO else "pr_gate"
        elif band is Band.REVIEW:
            nxt = "review_gate"
        else:
            reworks += 1
            nxt = pack.rework_role if reworks <= pack.max_rework else "review_gate"
        record = {**verdict.model_dump(), "band": band.value}
        events.append(mission.ticket, "verdict", {**record, "next": nxt}, role="judge")
        update: MissionState = {"trail": ["judge"], "verdict": record, "next": nxt, "reworks": reworks}
        if nxt == pack.rework_role:
            # The coder starts from what the judge actually saw, not from the last role's claims.
            failed = ", ".join(verdict.failed_checks) or "none"
            handoff = Handoff(
                from_role="judge",
                summary=f"The judge sent this lane back (band {band.value}). Failed checks: {failed}.\n\n{verdict.rationale}",
                send_back=True,
            )
            if recorder is not None:
                sha = recorder.record(mission, len(state.get("handoffs", [])) + 1, handoff, nxt)
                handoff = handoff.model_copy(update={"commit_sha": sha})
            update["handoffs"] = [handoff.model_dump()]
        return update

    def gate_node(kind: GateKind, on_approve: Callable[[MissionState], str], on_send_back: Callable[[MissionState], str]):
        # A gate node re-runs from the top when resumed, so nothing before interrupt() may have side effects.
        def run(state: MissionState) -> MissionState:
            mission = _mission(state)
            if kind is GateKind.SPEC and mission.autonomy is Autonomy.AUTO:
                return {"next": on_approve(state)}
            trail = state.get("trail", [])
            answer = interrupt({"kind": kind.value, "after": trail[-1] if trail else None, "verdict": state.get("verdict")})
            if answer == APPROVE:
                return {"next": on_approve(state)}
            note = Handoff(from_role="human", summary=f"Sent back at the {kind.value} gate.", send_back=True)
            return {"next": on_send_back(state), "handoffs": [note.model_dump()]}

        return run

    def done_node(state: MissionState) -> MissionState:
        mission = _mission(state)
        url = None
        if publisher is not None:
            handoffs = [Handoff.model_validate(h) for h in state.get("handoffs", [])]
            url = publisher.publish(mission, handoffs, state.get("verdict"))
            events.append(mission.ticket, "pr.opened", {"url": url})
        events.append(mission.ticket, "mission.done", {"status": "pr_ready", "pr_url": url})
        return {"status": "pr_ready", "pr_url": url}

    # Approving a review-band verdict is "approve and open PR", so it skips the PR gate.
    gates = {
        "spec_gate": gate_node(GateKind.SPEC, lambda s: pack.lane_roles[0], lambda s: pack.planning_roles[0]),
        "handoff_gate": gate_node(GateKind.HANDOFF, lambda s: s["next"], lambda s: s["trail"][-1]),
        "review_gate": gate_node(GateKind.REVIEW, lambda s: "done", lambda s: pack.rework_role),
        "pr_gate": gate_node(GateKind.PR, lambda s: "done", lambda s: pack.rework_role),
    }

    def after_role(state: MissionState) -> str:
        incomplete = bool(state.get("handoffs")) and state["handoffs"][-1].get("incomplete")
        # A step without a real handoff always waits for a human, whatever the autonomy.
        return "handoff_gate" if incomplete or _mission(state).autonomy is Autonomy.MANUAL else state["next"]

    def follow_next(state: MissionState) -> str:
        return state["next"]

    for role in (*pack.planning_roles, *pack.lane_roles):
        graph.add_node(role, role_node(role))
        graph.add_conditional_edges(role, after_role)
    graph.add_node("judge", judge_node)
    graph.add_conditional_edges("judge", follow_next)
    for name, node in gates.items():
        graph.add_node(name, node)
        graph.add_conditional_edges(name, follow_next)
    graph.add_node("done", done_node)
    graph.add_edge(START, (pack.planning_roles or pack.lane_roles)[0])
    graph.add_edge("done", END)
    return graph
