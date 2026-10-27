"""Run lifecycle: start, pause for approval, resume, finish. Persists every step."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import defaultdict
from typing import Any

from opscopilot.agents.context import AgentContext
from opscopilot.agents.graph import Graph, GraphError
from opscopilot.agents.specialist import AgentError, note_injection
from opscopilot.agents.state import RunState, TicketView
from opscopilot.agents.workflow import build_graph
from opscopilot.config import Settings
from opscopilot.db.pool import Database
from opscopilot.guardrails.budget import BudgetExceededError, BudgetLimits
from opscopilot.guardrails.policy import PolicyConfig, TrustedFacts
from opscopilot.llm.base import LLMClient, LLMError
from opscopilot.runtime.gateway import ToolGateway
from opscopilot.telemetry import run_trace_context, tracer

TERMINAL = {"completed", "failed", "budget_exceeded"}


class RunNotFoundError(LookupError):
    pass


class ApprovalConflictError(RuntimeError):
    pass


class _Events:
    def __init__(self, db: Database, run_id: str) -> None:
        self.db, self.run_id = db, run_id

    async def emit(
        self,
        kind: str,
        agent: str | None,
        payload: dict[str, Any],
        duration_ms: float | None = None,
    ) -> None:
        await self.db.execute(
            "INSERT INTO run_events (run_id, kind, agent, payload, duration_ms)"
            " VALUES ($1, $2, $3, $4, $5)",
            uuid.UUID(self.run_id), kind, agent, payload, duration_ms,
        )  # fmt: skip


class _Approvals:
    def __init__(self, db: Database, run_id: str) -> None:
        self.db, self.run_id = db, run_id

    async def create(self, agent: str, tool: str, args: dict[str, Any], reason: str) -> str:
        approval_id = await self.db.fetchval(
            "INSERT INTO approvals (run_id, agent, tool, args, reason)"
            " VALUES ($1, $2, $3, $4, $5) RETURNING id",
            uuid.UUID(self.run_id), agent, tool, args, reason,
        )  # fmt: skip
        return str(approval_id)

    async def get(self, approval_id: str) -> dict[str, Any]:
        row = await self.db.fetchrow(
            "SELECT status, decision_note FROM approvals WHERE id = $1", uuid.UUID(approval_id)
        )
        if row is None:
            raise RunNotFoundError(f"approval {approval_id} not found")
        return row


class RunService:
    def __init__(
        self, db: Database, gateway: ToolGateway, llm: LLMClient, settings: Settings
    ) -> None:
        self.db, self.gateway, self.llm, self.settings = db, gateway, llm, settings
        self.graph: Graph[AgentContext] = build_graph()
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.policy = PolicyConfig(
            approval_threshold_cents=settings.refund_approval_threshold_cents,
            hard_cap_cents=settings.refund_hard_cap_cents,
            support_email=settings.support_email,
        )
        self.limits = BudgetLimits(
            max_llm_calls=settings.max_llm_calls,
            max_tool_calls=settings.max_tool_calls,
            max_total_tokens=settings.max_total_tokens,
            max_cost_usd=settings.max_cost_usd,
        )

    # -- public API -----------------------------------------------------------------------

    async def create_run(self, ticket_id: str) -> str:
        """Load the ticket over MCP, scan it, and persist a new run (not yet executed)."""
        loaded = await self.gateway.call("ticketing.get_ticket", {"ticket_id": ticket_id},
                                         runtime_args={})  # fmt: skip
        if not loaded.ok:
            raise RunNotFoundError(loaded.text)
        ticket = TicketView.model_validate(loaded.data)
        run_id = str(uuid.uuid4())
        state = RunState(
            run_id=run_id,
            ticket=ticket,
            path=["supervisor"],
            facts=TrustedFacts(ticket_id=ticket.id, requester_email=ticket.customer_email),
        )
        report = note_injection(state, f"ticket:{ticket.id}", f"{ticket.subject}\n{ticket.body}")
        await self.db.execute(
            "INSERT INTO runs (id, ticket_id, status, provider, model, state, trace_id)"
            " VALUES ($1, $2, 'running', $3, $4, $5, $6)",
            uuid.UUID(run_id), ticket.id, self.llm.provider, self.llm.model,
            state.model_dump(mode="json"), f"{uuid.UUID(run_id).int:032x}",
        )  # fmt: skip
        events = _Events(self.db, run_id)
        await events.emit("run_start", None, {"ticket_id": ticket.id, "model": self.llm.model})
        if report.suspected:
            await events.emit(
                "guardrail",
                None,
                {"rule": "injection-detected", "source": f"ticket:{ticket.id}",
                 "signals": report.signals, "score": report.score},
            )  # fmt: skip
        return run_id

    async def start(self, ticket_id: str) -> str:
        run_id = await self.create_run(ticket_id)
        await self.advance(run_id)
        return run_id

    async def decide(
        self, approval_id: str, *, approve: bool, reviewer: str, note: str | None
    ) -> str:
        row = await self.db.fetchrow(
            "UPDATE approvals SET status = $2, reviewer = $3, decision_note = $4,"
            " decided_at = now() WHERE id = $1 AND status = 'pending' RETURNING run_id",
            uuid.UUID(approval_id), "approved" if approve else "rejected", reviewer, note,
        )  # fmt: skip
        if row is None:
            raise ApprovalConflictError("approval not found or already decided")
        return str(row["run_id"])

    async def advance(self, run_id: str) -> str:
        """Run the graph until it finishes or pauses. Safe to call repeatedly."""
        async with self._locks[run_id]:
            row = await self.db.fetchrow(
                "SELECT status, state FROM runs WHERE id = $1", uuid.UUID(run_id)
            )
            if row is None:
                raise RunNotFoundError(run_id)
            if row["status"] in TERMINAL:
                return str(row["status"])
            state = RunState.model_validate(row["state"])
            state.status = "running"
            return await self._execute(state)

    # -- internals ----------------------------------------------------------------------

    async def _execute(self, state: RunState) -> str:
        events = _Events(self.db, state.run_id)
        ctx = AgentContext(
            llm=self.llm,
            gateway=self.gateway,
            policy=self.policy,
            limits=self.limits,
            price_in_per_mtok=self.settings.llm_input_cost_per_mtok,
            price_out_per_mtok=self.settings.llm_output_cost_per_mtok,
            events=events,
            approvals=_Approvals(self.db, state.run_id),
            max_ticket_chars=self.settings.max_ticket_chars,
        )
        t0 = time.perf_counter()
        with tracer.start_as_current_span(
            "run_segment", context=run_trace_context(uuid.UUID(state.run_id))
        ) as span:
            span.set_attribute("opscopilot.run_id", state.run_id)
            span.set_attribute("opscopilot.ticket_id", state.ticket.id)
            try:
                result = await self.graph.run(state, ctx, self._checkpoint)
                state.status = "awaiting_approval" if result == "paused" else "completed"
            except BudgetExceededError as exc:
                state.status, state.error = "budget_exceeded", str(exc)
            except (AgentError, GraphError, LLMError) as exc:
                state.status, state.error = "failed", f"{type(exc).__name__}: {exc}"
            span.set_attribute("opscopilot.status", state.status)
            span.set_attribute("gen_ai.usage.input_tokens", state.usage.tokens_in)
            span.set_attribute("gen_ai.usage.output_tokens", state.usage.tokens_out)
            span.set_attribute("opscopilot.cost_usd", state.usage.cost_usd)
        outcome = await self._outcome(state) if state.status in TERMINAL else None
        await self._checkpoint(state, outcome=outcome)
        if state.status in TERMINAL:
            await events.emit(
                "run_finish",
                None,
                {"status": state.status, "error": state.error, "outcome": outcome},
                (time.perf_counter() - t0) * 1000,
            )
        return state.status

    async def _checkpoint(self, state: RunState, outcome: dict[str, Any] | None = None) -> None:
        u = state.usage
        await self.db.execute(
            "UPDATE runs SET state = $2, status = $3, outcome = COALESCE($4, outcome),"
            " llm_calls = $5, tool_calls = $6, tokens_in = $7, tokens_out = $8, cost_usd = $9,"
            " updated_at = now() WHERE id = $1",
            uuid.UUID(state.run_id), state.model_dump(mode="json"), state.status, outcome,
            u.llm_calls, u.tool_calls, u.tokens_in, u.tokens_out, u.cost_usd,
        )  # fmt: skip

    async def _outcome(self, state: RunState) -> dict[str, Any]:
        ticket = await self.db.fetchrow("SELECT status FROM tickets WHERE id = $1", state.ticket.id)
        refunds = await self.db.fetch(
            "SELECT order_id, amount_cents, approval_id IS NOT NULL AS human_approved"
            " FROM refunds WHERE idempotency_key LIKE $1 ORDER BY created_at",
            f"{state.run_id}:%",
        )
        outcome: dict[str, Any] = json.loads(
            json.dumps(
                {
                    "ticket_status": ticket["status"] if ticket else None,
                    "refunds": refunds,
                    "resolution": state.findings.get("resolution"),
                    "injection_flagged": state.tainted,
                    "path": state.path,
                },
                default=str,
            )
        )
        return outcome
