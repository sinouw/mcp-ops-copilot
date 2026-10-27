"""API smoke test with real stdio MCP subprocesses, as the server runs in production."""

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
from asgi_lifespan import LifespanManager

from opscopilot.api.app import create_app
from opscopilot.config import Settings
from opscopilot.db.pool import Database

pytestmark = pytest.mark.integration


@pytest.fixture
async def client(db: Database, settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings.model_copy(update={"reviewer_token": "secret"}))
    async with LifespanManager(app, startup_timeout=60):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            yield c


async def _wait(client: httpx.AsyncClient, run_id: str, status: str) -> dict[str, object]:
    for _ in range(100):
        run = (await client.get(f"/api/runs/{run_id}")).json()
        if run["status"] == status:
            return dict(run)
        await asyncio.sleep(0.1)
    raise AssertionError(f"run never reached {status}: {run['status']}")


async def test_full_approval_flow_over_http(client: httpx.AsyncClient) -> None:
    health = await client.get("/api/health")
    assert health.status_code == 200, health.text
    assert health.json()["ok"]
    assert len((await client.get("/api/tickets")).json()) == 16

    run_id = (await client.post("/api/tickets/T-1002/runs")).json()["run_id"]
    await _wait(client, run_id, "awaiting_approval")
    (approval,) = (await client.get("/api/approvals")).json()
    assert approval["tool"] == "ticketing.send_reply" and approval["ticket_id"] == "T-1002"

    url = f"/api/approvals/{approval['id']}/decision"
    denied = await client.post(url, json={"approve": True})
    assert denied.status_code == 401
    ok = await client.post(url, json={"approve": True}, headers={"x-reviewer-token": "secret"})
    assert ok.status_code == 200
    again = await client.post(url, json={"approve": True}, headers={"x-reviewer-token": "secret"})
    assert again.status_code == 409

    run = await _wait(client, run_id, "completed")
    kinds = {e["kind"] for e in run["events"]}  # type: ignore[attr-defined]
    assert {"route", "llm", "policy", "tool_call", "approval_requested", "run_finish"} <= kinds


async def test_unknown_ticket_is_404(client: httpx.AsyncClient) -> None:
    assert (await client.post("/api/tickets/T-9999/runs")).status_code == 404
