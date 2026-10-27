"""Thin asyncpg wrapper shared by the API, the runtime and the MCP servers."""

from __future__ import annotations

import asyncio
import json
from importlib import resources
from typing import Any

import asyncpg


async def _init_connection(conn: asyncpg.Connection) -> None:
    for typ in ("json", "jsonb"):
        await conn.set_type_codec(typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


class Database:
    """Lazily-connected pool so MCP servers can be constructed before the event loop runs."""

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 10) -> None:
        self.dsn = dsn
        self._min, self._max = min_size, max_size
        self._pool: asyncpg.Pool | None = None
        self._lock = asyncio.Lock()

    async def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            async with self._lock:
                if self._pool is None:
                    self._pool = await asyncpg.create_pool(
                        self.dsn, min_size=self._min, max_size=self._max, init=_init_connection
                    )
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        pool = await self.pool()
        return [dict(r) for r in await pool.fetch(sql, *args)]

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        pool = await self.pool()
        row = await pool.fetchrow(sql, *args)
        return dict(row) if row is not None else None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        pool = await self.pool()
        return await pool.fetchval(sql, *args)

    async def execute(self, sql: str, *args: Any) -> str:
        pool = await self.pool()
        result: str = await pool.execute(sql, *args)
        return result


async def migrate(db: Database) -> None:
    sql = resources.files("opscopilot.db").joinpath("schema.sql").read_text()
    await db.execute(sql)
