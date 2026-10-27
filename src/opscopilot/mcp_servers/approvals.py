"""Server-side half of human-in-the-loop: MCP servers re-check approvals themselves.

The agent runtime decides *when* to ask a human, but a write tool never trusts the caller's
word that a human said yes. It loads the approval row, checks it is approved, unconsumed,
for this exact tool and these exact arguments, and consumes it in the same transaction as
the side effect. A bug or compromise in the runtime therefore cannot skip the review step.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg
from mcp.server.mcpserver.exceptions import ToolError

# Arguments the runtime injects; they are never part of what the reviewer approved.
RUNTIME_ARGS = frozenset({"approval_id", "idempotency_key"})


def approved_view(args: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in args.items() if k not in RUNTIME_ARGS}


async def consume_approval(
    conn: asyncpg.Connection, approval_id: str | None, tool: str, args: dict[str, Any]
) -> UUID:
    if not approval_id:
        raise ToolError(f"{tool} requires a human approval_id; none was provided.")
    try:
        key = UUID(approval_id)
    except ValueError as exc:
        raise ToolError("approval_id is not a valid UUID.") from exc
    row = await conn.fetchrow("SELECT * FROM approvals WHERE id = $1 FOR UPDATE", key)
    if row is None:
        raise ToolError("approval not found.")
    if row["status"] != "approved":
        raise ToolError(f"approval is {row['status']}, not approved.")
    if row["consumed_at"] is not None:
        raise ToolError("approval was already used.")
    if row["tool"] != tool or row["args"] != approved_view(args):
        raise ToolError("approval does not match this tool call's arguments.")
    await conn.execute("UPDATE approvals SET consumed_at = now() WHERE id = $1", key)
    return key
