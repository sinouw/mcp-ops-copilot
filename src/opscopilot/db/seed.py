"""Reset and seed the demo dataset."""

from __future__ import annotations

from importlib import resources
from typing import Any

import yaml

from opscopilot.db.pool import Database, migrate

_TABLES = "approvals, run_events, runs, ticket_messages, tickets, refunds, orders, customers"


def load_seed() -> dict[str, list[dict[str, Any]]]:
    text = resources.files("opscopilot.db").joinpath("seed_data.yaml").read_text()
    data: dict[str, list[dict[str, Any]]] = yaml.safe_load(text)
    return data


async def reset_and_seed(db: Database) -> None:
    await migrate(db)
    data = load_seed()
    pool = await db.pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(f"TRUNCATE {_TABLES} RESTART IDENTITY CASCADE")
        for c in data["customers"]:
            await conn.execute(
                "INSERT INTO customers (id, name, email, phone, tier, notes)"
                " VALUES ($1, $2, $3, $4, $5, $6)",
                c["id"], c["name"], c["email"], c.get("phone"),
                c.get("tier", "standard"), c.get("notes", ""),
            )  # fmt: skip
        for o in data["orders"]:
            await conn.execute(
                "INSERT INTO orders (id, customer_id, status, total_cents, items, carrier, eta)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7::date)",
                o["id"], o["customer_id"], o["status"], o["total_cents"], o["items"],
                o.get("carrier"), _date(o.get("eta")),
            )  # fmt: skip
        for r in data["refunds"]:
            await conn.execute(
                "INSERT INTO refunds (order_id, amount_cents, reason, idempotency_key)"
                " VALUES ($1, $2, $3, $4)",
                r["order_id"], r["amount_cents"], r["reason"], r["idempotency_key"],
            )  # fmt: skip
        for t in data["tickets"]:
            await conn.execute(
                "INSERT INTO tickets (id, customer_email, subject, body) VALUES ($1, $2, $3, $4)",
                t["id"], t["customer_email"], t["subject"], t["body"],
            )  # fmt: skip


def _date(value: Any) -> Any:
    import datetime as dt

    if value is None or isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value))
