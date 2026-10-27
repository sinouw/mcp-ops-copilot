"""CRM / orders MCP server: customers, orders and refunds backed by Postgres."""

from __future__ import annotations

import os
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from opscopilot.db.pool import Database
from opscopilot.mcp_servers.approvals import consume_approval
from opscopilot.mcp_servers.common import run_server

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)

OrderId = Annotated[str, Field(pattern=r"^O-\d{4,8}$", description="Order id, e.g. O-5001")]
CustomerId = Annotated[str, Field(pattern=r"^C-\d{3,8}$", description="Customer id, e.g. C-001")]

_ORDER_SQL = """
SELECT o.id, o.customer_id, o.status, o.total_cents, o.currency, o.items, o.carrier,
       to_char(o.eta, 'YYYY-MM-DD') AS eta, to_char(o.placed_at, 'YYYY-MM-DD') AS placed_at,
       COALESCE((SELECT sum(amount_cents) FROM refunds r WHERE r.order_id = o.id), 0)::int
         AS refunded_cents
FROM orders o
"""


def _mask_phone(phone: str | None) -> str | None:
    return None if not phone else "***" + phone[-4:]


def _customer(row: dict[str, Any]) -> dict[str, Any]:
    # Data minimisation: agents never see full phone numbers.
    return {
        "id": row["id"],
        "name": row["name"],
        "email": row["email"],
        "phone": _mask_phone(row["phone"]),
        "tier": row["tier"],
        "notes": row["notes"],
    }


def _order(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["refundable_cents"] = max(0, row["total_cents"] - row["refunded_cents"])
    return out


def build_crm_server(
    db: Database,
    *,
    approval_threshold_cents: int,
    hard_cap_cents: int,
) -> MCPServer:
    server = MCPServer(
        "crm",
        instructions="Customer, order and refund operations for Acme Shop support.",
        version="0.1.0",
    )

    @server.tool(annotations=READ)
    async def find_customer_by_email(
        email: Annotated[str, Field(max_length=254)],
    ) -> dict[str, Any]:
        """Look up a customer by their email address."""
        row = await db.fetchrow("SELECT * FROM customers WHERE lower(email) = lower($1)", email)
        if row is None:
            raise ToolError(f"no customer with email {email}")
        return _customer(row)

    @server.tool(annotations=READ)
    async def get_customer(customer_id: CustomerId) -> dict[str, Any]:
        """Fetch a customer by id."""
        row = await db.fetchrow("SELECT * FROM customers WHERE id = $1", customer_id)
        if row is None:
            raise ToolError(f"customer {customer_id} not found")
        return _customer(row)

    @server.tool(annotations=READ)
    async def list_orders(customer_id: CustomerId) -> dict[str, Any]:
        """List a customer's orders with refunded and still-refundable amounts (cents)."""
        rows = await db.fetch(_ORDER_SQL + " WHERE o.customer_id = $1 ORDER BY o.id", customer_id)
        return {"customer_id": customer_id, "orders": [_order(r) for r in rows]}

    @server.tool(annotations=READ)
    async def get_order(order_id: OrderId) -> dict[str, Any]:
        """Fetch one order, including its owner, status, shipping and refund totals (cents)."""
        row = await db.fetchrow(_ORDER_SQL + " WHERE o.id = $1", order_id)
        if row is None:
            raise ToolError(f"order {order_id} not found")
        return _order(row)

    @server.tool(annotations=WRITE)
    async def issue_refund(
        order_id: OrderId,
        amount_cents: Annotated[int, Field(gt=0, description="Refund amount in cents")],
        reason: Annotated[str, Field(min_length=3, max_length=500)],
        idempotency_key: Annotated[str, Field(min_length=8, max_length=128)],
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        """Refund part or all of an order. Large refunds need an approved human review."""
        pool = await db.pool()
        async with pool.acquire() as conn, conn.transaction():
            existing = await conn.fetchrow(
                "SELECT id, order_id, amount_cents FROM refunds WHERE idempotency_key = $1",
                idempotency_key,
            )
            if existing is not None:
                return {"refund_id": str(existing["id"]), "status": "duplicate_ignored"}
            order = await conn.fetchrow(_ORDER_SQL + " WHERE o.id = $1 FOR UPDATE OF o", order_id)
            if order is None:
                raise ToolError(f"order {order_id} not found")
            if order["status"] == "cancelled":
                raise ToolError("cancelled orders cannot be refunded")
            refundable = order["total_cents"] - order["refunded_cents"]
            if amount_cents > refundable:
                raise ToolError(
                    f"amount {amount_cents} exceeds refundable balance {refundable} cents"
                )
            if amount_cents > hard_cap_cents:
                raise ToolError(f"amount exceeds the hard cap of {hard_cap_cents} cents")
            approval = None
            if amount_cents > approval_threshold_cents:
                args = {"order_id": order_id, "amount_cents": amount_cents, "reason": reason}
                approval = await consume_approval(conn, approval_id, "crm.issue_refund", args)
            refund_id = await conn.fetchval(
                "INSERT INTO refunds (order_id, amount_cents, reason, idempotency_key, approval_id)"
                " VALUES ($1, $2, $3, $4, $5) RETURNING id",
                order_id, amount_cents, reason, idempotency_key, approval,
            )  # fmt: skip
        return {
            "refund_id": str(refund_id),
            "status": "refunded",
            "order_id": order_id,
            "amount_cents": amount_cents,
            "remaining_refundable_cents": refundable - amount_cents,
        }

    return server


def main() -> None:
    from opscopilot.config import get_settings

    s = get_settings()
    server = build_crm_server(
        Database(os.environ.get("DATABASE_URL", s.database_url)),
        approval_threshold_cents=s.refund_approval_threshold_cents,
        hard_cap_cents=s.refund_hard_cap_cents,
    )
    run_server(server, default_port=8101)


if __name__ == "__main__":
    main()
