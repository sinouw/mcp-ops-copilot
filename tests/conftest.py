from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import AsyncIterator, Iterator

import pytest

from opscopilot.config import Settings
from opscopilot.db.pool import Database
from opscopilot.db.seed import reset_and_seed
from opscopilot.mcp_servers.crm import build_crm_server
from opscopilot.mcp_servers.ticketing import build_ticketing_server
from opscopilot.runtime.gateway import ToolGateway


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """TEST_DATABASE_URL (CI service container) or an embedded throwaway Postgres."""
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        yield url
        return
    import pgserver

    with tempfile.TemporaryDirectory() as tmp:
        server = pgserver.get_server(tmp, cleanup_mode="stop")
        yield server.get_uri()
        server.cleanup()


@pytest.fixture(scope="session")
def settings(database_url: str) -> Settings:
    return Settings(database_url=database_url, llm_provider="scripted", _env_file=None)  # type: ignore[call-arg]


@pytest.fixture
async def db(database_url: str) -> AsyncIterator[Database]:
    database = Database(database_url, max_size=5)
    await reset_and_seed(database)
    yield database
    await database.close()


@pytest.fixture
async def gateway(db: Database, settings: Settings) -> AsyncIterator[ToolGateway]:
    """MCP servers connected in-process (same protocol, no subprocess)."""
    crm = build_crm_server(
        db,
        approval_threshold_cents=settings.refund_approval_threshold_cents,
        hard_cap_cents=settings.refund_hard_cap_cents,
    )
    # anyio cancel scopes must be entered and exited in the same task, but pytest-asyncio may
    # tear fixtures down from another task, so a helper task owns the gateway's lifetime.
    ready, stop = asyncio.Event(), asyncio.Event()
    holder: list[ToolGateway] = []

    async def hold() -> None:
        async with ToolGateway({"crm": crm, "ticketing": build_ticketing_server(db)}) as gw:
            holder.append(gw)
            ready.set()
            await stop.wait()

    task = asyncio.create_task(hold())
    await ready.wait()
    yield holder[0]
    stop.set()
    await task
