"""The ops-copilot graph: a supervisor routing between three scoped specialists."""

from __future__ import annotations

import json

from opscopilot.agents import prompts
from opscopilot.agents.context import AgentContext
from opscopilot.agents.graph import Goto, Graph, Step
from opscopilot.agents.llm_call import chat
from opscopilot.agents.specialist import Specialist
from opscopilot.agents.state import END, RunState
from opscopilot.guardrails.injection import wrap_untrusted
from opscopilot.llm.base import Message, ToolSpec
from opscopilot.telemetry import tracer

SPECIALISTS = ["triage", "research", "resolution"]
EDGES: dict[str, list[str]] = {
    "supervisor": [*SPECIALISTS, END],
    **{name: ["supervisor"] for name in SPECIALISTS},
}
MAX_VISITS = 2


def allowed_next(state: RunState) -> list[str]:
    """Hard constraints on routing. The supervisor model only chooses within these."""
    f = state.findings
    if "triage" not in f:
        return ["triage"]
    if "research" not in f:
        # Pure feedback may not need the CRM at all: a genuine judgement call for the model.
        return ["research", END] if f["triage"].get("category") == "feedback" else ["research"]
    if "resolution" not in f:
        return ["resolution"]
    return [END]


async def supervisor(state: RunState, ctx: AgentContext) -> Step:
    options = allowed_next(state)
    with tracer.start_as_current_span("invoke_agent supervisor") as span:
        span.set_attribute("gen_ai.agent.name", "supervisor")
        span.set_attribute("opscopilot.route_options", options)
        if len(options) == 1:
            choice, reason, decided_by = options[0], "only valid next step", "rules"
        else:
            choice, reason = await _ask_model(state, ctx, options)
            decided_by = "model"
        if choice != END and state.visits.get(choice, 0) >= MAX_VISITS:
            choice, reason, decided_by = END, f"{choice} already ran {MAX_VISITS} times", "rules"
        span.set_attribute("opscopilot.route", choice)
    state.visits[choice] = state.visits.get(choice, 0) + 1
    if choice in SPECIALISTS:
        # A specialist always starts from fresh context when (re)entered.
        state.sessions.pop(choice, None)
    await ctx.events.emit(
        "route", "supervisor", {"next": choice, "reason": reason, "decided_by": decided_by,
                                "options": options},
    )  # fmt: skip
    return Goto(choice)


async def _ask_model(state: RunState, ctx: AgentContext, options: list[str]) -> tuple[str, str]:
    names = ["finish" if o == END else o for o in options]
    route = ToolSpec(
        name="route",
        description="Choose the next step.",
        parameters={
            "type": "object",
            "properties": {
                "next": {"type": "string", "enum": names},
                "reason": {"type": "string"},
            },
            "required": ["next", "reason"],
        },
    )
    messages = [
        Message(role="system", content=prompts.SUPERVISOR),
        Message(
            role="user",
            content=f"Allowed next steps: {names}\nFindings so far (JSON):\n"
            + wrap_untrusted(json.dumps(state.findings), "findings", max_chars=8000),
        ),
    ]
    resp = await chat(state, ctx, "supervisor", messages, [route])
    for call in resp.message.tool_calls:
        if call.name == "route" and call.arguments.get("next") in names:
            nxt = str(call.arguments["next"])
            return (END if nxt == "finish" else nxt), str(call.arguments.get("reason", ""))[:300]
    await ctx.events.emit("guardrail", "supervisor", {"rule": "invalid-route"})
    return options[0], "model returned no valid route; using default"


def build_graph() -> Graph[AgentContext]:
    triage = Specialist("triage", prompts.TRIAGE, sees_ticket=True)
    # Research never sees raw ticket text: it works from triage's validated structured output,
    # which shrinks the injection surface for the agent that reads customer records.
    research = Specialist("research", prompts.RESEARCH, sees_ticket=False)
    resolution = Specialist("resolution", prompts.RESOLUTION, sees_ticket=True)
    return Graph(
        nodes={
            "supervisor": supervisor,
            "triage": triage,
            "research": research,
            "resolution": resolution,
        },
        edges=EDGES,
    )
