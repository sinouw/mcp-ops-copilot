"""HTTP API for the approval console."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from opscopilot.agents.state import END
from opscopilot.agents.workflow import EDGES
from opscopilot.config import Settings, get_settings
from opscopilot.db.pool import Database, migrate
from opscopilot.db.seed import reset_and_seed
from opscopilot.guardrails.policy import AGENT_SCOPES
from opscopilot.llm.factory import create_llm
from opscopilot.runtime.bootstrap import build_gateway
from opscopilot.runtime.service import ApprovalConflictError, RunNotFoundError, RunService
from opscopilot.telemetry import setup_tracing

log = logging.getLogger("opscopilot.api")


class Decision(BaseModel):
    approve: bool
    reviewer: str = Field(default="reviewer", min_length=1, max_length=80)
    note: str | None = Field(default=None, max_length=1000)


class AppState:
    def __init__(self, db: Database, service: RunService, settings: Settings) -> None:
        self.db, self.service, self.settings = db, service, settings
        self.tasks: set[asyncio.Task[Any]] = set()

    def spawn(self, run_id: str) -> None:
        async def _go() -> None:
            try:
                await self.service.advance(run_id)
            except Exception:  # surfaced via run status; never kill the server
                log.exception("run %s crashed", run_id)

        task = asyncio.create_task(_go())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)


def _ops(request: Request) -> AppState:
    state: AppState = request.app.state.ops
    return state


Ops = Annotated[AppState, Depends(_ops)]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        setup_tracing(settings)
        db = Database(settings.database_url)
        await migrate(db)
        if not await db.fetchval("SELECT count(*) FROM tickets"):
            await reset_and_seed(db)
        async with build_gateway(settings) as gateway:
            app.state.ops = AppState(db, RunService(db, gateway, create_llm(settings), settings),
                                     settings)  # fmt: skip
            yield
            for task in list(app.state.ops.tasks):
                task.cancel()
        await db.close()

    app = FastAPI(title="mcp-ops-copilot", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    def reviewer_auth(x_reviewer_token: Annotated[str | None, Header()] = None) -> None:
        if settings.reviewer_token and x_reviewer_token != settings.reviewer_token:
            raise HTTPException(401, "invalid reviewer token")

    @app.get("/api/health")
    async def health(o: Ops) -> dict[str, Any]:
        await o.db.fetchval("SELECT 1")
        return {"ok": True, "model": o.service.llm.model, "provider": o.service.llm.provider}

    @app.get("/api/graph")
    async def graph() -> dict[str, Any]:
        return {
            "nodes": ["supervisor", "triage", "research", "resolution", END],
            "edges": [[src, dst] for src, dsts in EDGES.items() for dst in dsts],
            "scopes": {k: sorted(v) for k, v in AGENT_SCOPES.items()},
        }

    @app.get("/api/tickets")
    async def tickets(o: Ops) -> list[dict[str, Any]]:
        return await o.db.fetch(
            "SELECT t.id, t.customer_email, t.subject, t.status,"
            " (SELECT r.id::text FROM runs r WHERE r.ticket_id = t.id"
            "  ORDER BY r.created_at DESC LIMIT 1) AS last_run_id,"
            " (SELECT r.status FROM runs r WHERE r.ticket_id = t.id"
            "  ORDER BY r.created_at DESC LIMIT 1) AS last_run_status"
            " FROM tickets t ORDER BY t.id"
        )

    @app.post("/api/tickets/{ticket_id}/runs", status_code=202)
    async def start_run(ticket_id: str, o: Ops) -> dict[str, str]:
        try:
            run_id = await o.service.create_run(ticket_id)
        except RunNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        o.spawn(run_id)
        return {"run_id": run_id}

    @app.get("/api/runs")
    async def runs(o: Ops) -> list[dict[str, Any]]:
        return await o.db.fetch(
            "SELECT id::text, ticket_id, status, provider, model, llm_calls, tool_calls,"
            " tokens_in, tokens_out, cost_usd::float, trace_id, created_at, updated_at,"
            " (state->>'tainted')::bool AS tainted"
            " FROM runs ORDER BY created_at DESC LIMIT 100"
        )

    @app.get("/api/runs/{run_id}")
    async def run_detail(run_id: uuid.UUID, o: Ops) -> dict[str, Any]:
        run = await o.db.fetchrow(
            "SELECT id::text, ticket_id, status, provider, model, state, outcome, trace_id,"
            " llm_calls, tool_calls, tokens_in, tokens_out, cost_usd::float, created_at, updated_at"
            " FROM runs WHERE id = $1",
            run_id,
        )
        if run is None:
            raise HTTPException(404, "run not found")
        events = await o.db.fetch(
            "SELECT id, ts, kind, agent, payload, duration_ms FROM run_events"
            " WHERE run_id = $1 ORDER BY id",
            run_id,
        )
        approvals = await o.db.fetch(
            "SELECT id::text, agent, tool, args, reason, status, reviewer, decision_note,"
            " created_at, decided_at FROM approvals WHERE run_id = $1 ORDER BY created_at",
            run_id,
        )
        return {**run, "events": events, "approvals": approvals}

    @app.get("/api/approvals")
    async def approvals(o: Ops, status: str = "pending") -> list[dict[str, Any]]:
        return await o.db.fetch(
            "SELECT a.id::text, a.run_id::text, a.agent, a.tool, a.args, a.reason, a.status,"
            " a.created_at, r.ticket_id, t.subject, t.customer_email,"
            " r.state->'injections' AS injections"
            " FROM approvals a JOIN runs r ON r.id = a.run_id JOIN tickets t ON t.id = r.ticket_id"
            " WHERE a.status = $1 ORDER BY a.created_at",
            status,
        )

    @app.post("/api/approvals/{approval_id}/decision", dependencies=[Depends(reviewer_auth)])
    async def decide(approval_id: uuid.UUID, body: Decision, o: Ops) -> dict[str, str]:
        try:
            run_id = await o.service.decide(
                str(approval_id), approve=body.approve, reviewer=body.reviewer, note=body.note
            )
        except ApprovalConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        o.spawn(run_id)
        return {"run_id": run_id, "status": "approved" if body.approve else "rejected"}

    @app.post("/api/demo/reset", dependencies=[Depends(reviewer_auth)])
    async def demo_reset(o: Ops) -> dict[str, bool]:
        await reset_and_seed(o.db)
        return {"ok": True}

    return app
