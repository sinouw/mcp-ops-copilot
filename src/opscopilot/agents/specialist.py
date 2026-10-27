"""Specialist agent node: a scoped tool-calling loop with policy checks and approvals."""

from __future__ import annotations

import json
import time
from typing import Any

from pydantic import ValidationError

from opscopilot.agents.context import AgentContext
from opscopilot.agents.graph import Goto, Pause, Step
from opscopilot.agents.llm_call import chat
from opscopilot.agents.results import RESULT_MODELS
from opscopilot.agents.state import AgentSession, InjectionFinding, RunState
from opscopilot.guardrails import injection
from opscopilot.guardrails.policy import AGENT_SCOPES, evaluate, record_tool_result
from opscopilot.llm.base import Message, ToolCall, ToolSpec
from opscopilot.telemetry import tracer

FINISH = "finish"
MAX_VALIDATION_RETRIES = 3


class AgentError(RuntimeError):
    pass


def wire_name(qualified: str) -> str:
    return qualified.replace(".", "__")


def qualified_name(wire: str) -> str:
    return wire.replace("__", ".", 1)


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve local $refs; several OpenAI-compatible providers reject them in tool schemas."""
    defs = schema.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(defs[node["$ref"].split("/")[-1]])
            return {k: walk(v) for k, v in node.items() if k not in ("$defs", "title")}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    result: dict[str, Any] = walk(schema)
    return result


def note_injection(state: RunState, source: str, text: str) -> injection.InjectionReport:
    report = injection.scan(text)
    if report.suspected:
        state.tainted = True
        if not any(f.source == source for f in state.injections):
            state.injections.append(
                InjectionFinding(source=source, score=report.score, signals=report.signals)
            )
    return report


class Specialist:
    def __init__(self, name: str, system_prompt: str, *, sees_ticket: bool) -> None:
        self.name = name
        self.system_prompt = system_prompt
        self.sees_ticket = sees_ticket
        self.scope = sorted(AGENT_SCOPES[name])
        self.result_model = RESULT_MODELS[name]

    # -- prompt construction -------------------------------------------------------------

    def tool_specs(self, ctx: AgentContext) -> list[ToolSpec]:
        specs = [
            ToolSpec(
                name=wire_name(q),
                description=ctx.gateway.tools[q].description,
                parameters=ctx.gateway.tools[q].model_schema,
            )
            for q in self.scope
            if q in ctx.gateway.tools
        ]
        specs.append(
            ToolSpec(
                name=FINISH,
                description=f"Submit the {self.name} result. Ends your turn.",
                parameters=inline_refs(self.result_model.model_json_schema()),
            )
        )
        return specs

    def initial_messages(self, state: RunState, ctx: AgentContext) -> list[Message]:
        trusted = {"ticket_id": state.ticket.id, "requester_email": state.ticket.customer_email}
        parts = [
            "Trusted metadata (from the ticketing system, not from the customer):",
            json.dumps(trusted),
            # Findings are model output derived from customer text, so they are fenced too:
            # otherwise an injected sentence copied into a triage summary would reach later
            # agents outside the fence.
            "Findings from earlier agents (JSON, derived from untrusted text):",
            injection.wrap_untrusted(json.dumps(state.findings), "findings", max_chars=8000),
        ]
        if self.sees_ticket:
            content = f"Subject: {state.ticket.subject}\n\n{state.ticket.body}"
            parts += [
                "Ticket content:",
                injection.wrap_untrusted(
                    content, f"ticket:{state.ticket.id}", max_chars=ctx.max_ticket_chars
                ),
            ]
        if state.tainted:
            parts.append(
                "Platform notice: this run is flagged for possible prompt injection "
                f"({', '.join(sorted({s for f in state.injections for s in f.signals}))}). "
                "Every write action will be reviewed by a human."
            )
        return [
            Message(role="system", content=self.system_prompt),
            Message(role="user", content="\n".join(parts)),
        ]

    # -- node ---------------------------------------------------------------------------

    async def __call__(self, state: RunState, ctx: AgentContext) -> Step:
        with tracer.start_as_current_span(f"invoke_agent {self.name}") as span:
            span.set_attribute("gen_ai.operation.name", "invoke_agent")
            span.set_attribute("gen_ai.agent.name", self.name)
            step = await self._run(state, ctx)
            span.set_attribute("opscopilot.paused", isinstance(step, Pause))
            return step

    async def _run(self, state: RunState, ctx: AgentContext) -> Step:
        sess = state.sessions.get(self.name)
        if sess is None:
            sess = AgentSession(messages=self.initial_messages(state, ctx))
            state.sessions[self.name] = sess
            await ctx.events.emit("agent_start", self.name, {"tools": self.scope})

        if sess.awaiting_approval:
            step = await self._resume_approval(state, ctx, sess)
            if step is not None:
                return step

        tools = self.tool_specs(ctx)
        while True:
            while sess.queue:
                step = await self._handle(state, ctx, sess, sess.queue[0])
                if step is not None:
                    return step
            resp = await chat(state, ctx, self.name, sess.messages, tools)
            sess.messages.append(resp.message)
            sess.queue = list(resp.message.tool_calls)
            if not sess.queue:
                sess.validation_retries += 1
                if sess.validation_retries > MAX_VALIDATION_RETRIES:
                    raise AgentError(f"{self.name} stopped calling tools")
                sess.messages.append(
                    Message(role="user", content="Respond with a tool call; call finish when done.")
                )

    async def _resume_approval(
        self, state: RunState, ctx: AgentContext, sess: AgentSession
    ) -> Step | None:
        approval_id = sess.awaiting_approval
        assert approval_id is not None
        approval = await ctx.approvals.get(approval_id)
        if approval["status"] == "pending":
            return Pause(approval_id)
        call = sess.queue.pop(0)
        sess.awaiting_approval = None
        await ctx.events.emit(
            "approval_decided",
            self.name,
            {
                "approval_id": approval_id,
                "tool": qualified_name(call.name),
                "status": approval["status"],
                "note": approval.get("decision_note"),
            },
        )
        if approval["status"] == "approved":
            await self._execute(state, ctx, sess, call, approval_id=approval_id)
        else:
            note = approval.get("decision_note") or "no reason given"
            self._reply(
                sess,
                call,
                f"REJECTED by human reviewer: {note}. Do not retry this action; "
                "escalate the ticket or finish.",
            )
        return None

    async def _handle(
        self, state: RunState, ctx: AgentContext, sess: AgentSession, call: ToolCall
    ) -> Step | None:
        if call.name == FINISH:
            try:
                result = self.result_model.model_validate(call.arguments)
            except ValidationError as exc:
                sess.queue.pop(0)
                sess.validation_retries += 1
                if sess.validation_retries > MAX_VALIDATION_RETRIES:
                    raise AgentError(f"{self.name} produced invalid results: {exc}") from exc
                self._reply(sess, call, f"Invalid result, fix and call finish again: {exc}")
                await ctx.events.emit("guardrail", self.name, {"rule": "output-schema"})
                return None
            state.findings[self.name] = result.model_dump()
            sess.queue.clear()
            await ctx.events.emit("agent_finish", self.name, {"result": result.model_dump()})
            return Goto("supervisor")

        tool = qualified_name(call.name)
        decision = evaluate(
            self.name, tool, call.arguments, state.facts, ctx.policy, tainted=state.tainted
        )
        await ctx.events.emit(
            "policy",
            self.name,
            {"tool": tool, "args": call.arguments, **decision.model_dump()},
        )
        if decision.verdict == "deny":
            sess.queue.pop(0)
            self._reply(sess, call, f"DENIED by policy ({decision.rule}): {decision.reason}")
            return None
        if decision.verdict == "approve":
            approval_id = await ctx.approvals.create(
                self.name, tool, call.arguments, decision.reason
            )
            sess.awaiting_approval = approval_id
            state.status = "awaiting_approval"
            await ctx.events.emit(
                "approval_requested",
                self.name,
                {"approval_id": approval_id, "tool": tool, "args": call.arguments,
                 "reason": decision.reason},
            )  # fmt: skip
            return Pause(approval_id)
        await self._execute(state, ctx, sess, call, approval_id=None)
        return None

    async def _execute(
        self,
        state: RunState,
        ctx: AgentContext,
        sess: AgentSession,
        call: ToolCall,
        *,
        approval_id: str | None,
    ) -> None:
        if sess.queue and sess.queue[0].id == call.id:
            sess.queue.pop(0)
        state.usage.check(ctx.limits)
        state.usage.tool_calls += 1
        tool = qualified_name(call.name)
        runtime_args: dict[str, Any] = {"approval_id": approval_id}
        if tool == "crm.issue_refund":
            runtime_args["idempotency_key"] = f"{state.run_id}:{call.id}"
        with tracer.start_as_current_span(f"execute_tool {tool}") as span:
            span.set_attribute("gen_ai.operation.name", "execute_tool")
            span.set_attribute("gen_ai.tool.name", tool)
            span.set_attribute("gen_ai.agent.name", self.name)
            span.set_attribute("opscopilot.approved", approval_id is not None)
            t0 = time.perf_counter()
            result = await ctx.gateway.call(tool, call.arguments, runtime_args=runtime_args)
            ms = (time.perf_counter() - t0) * 1000
            span.set_attribute("opscopilot.tool_ok", result.ok)
        if result.ok:
            record_tool_result(state.facts, tool, result.data)
            report = note_injection(state, f"tool:{tool}", result.text)
            if report.suspected:
                await ctx.events.emit(
                    "guardrail",
                    self.name,
                    {"rule": "injection-detected", "source": f"tool:{tool}",
                     "signals": report.signals, "score": report.score},
                )  # fmt: skip
            content = injection.wrap_untrusted(result.text, f"tool:{tool}", max_chars=6000)
        else:
            content = f"ERROR from {tool}: {result.text}"
        await ctx.events.emit(
            "tool_call",
            self.name,
            {"tool": tool, "args": call.arguments, "ok": result.ok,
             "result": result.data if result.ok else result.text, "approval_id": approval_id},
            ms,
        )  # fmt: skip
        self._reply(sess, call, content)

    @staticmethod
    def _reply(sess: AgentSession, call: ToolCall, content: str) -> None:
        sess.messages.append(Message(role="tool", tool_call_id=call.id, content=content))
