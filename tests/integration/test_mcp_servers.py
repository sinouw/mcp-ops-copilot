"""The MCP servers enforce approvals themselves, independent of the agent runtime."""

import uuid

import pytest

from opscopilot.db.pool import Database
from opscopilot.runtime.gateway import ToolGateway

pytestmark = pytest.mark.integration


async def _approval(
    db: Database, tool: str, args: dict[str, object], status: str = "approved"
) -> str:
    run_id = await db.fetchval(
        "INSERT INTO runs (ticket_id, status, provider, model, state)"
        " VALUES ('T-1003', 'running', 'test', 'test', '{}') RETURNING id"
    )
    approval_id = await db.fetchval(
        "INSERT INTO approvals (run_id, agent, tool, args, reason, status)"
        " VALUES ($1, 'resolution', $2, $3, 'test', $4) RETURNING id",
        run_id, tool, args, status,
    )  # fmt: skip
    return str(approval_id)


REFUND = {"order_id": "O-5003", "amount_cents": 18000, "reason": "defective"}


async def _refund(gw: ToolGateway, approval_id: str | None, key: str | None = None) -> object:
    return await gw.call(
        "crm.issue_refund",
        REFUND,
        runtime_args={"approval_id": approval_id, "idempotency_key": key or uuid.uuid4().hex},
    )


async def test_tools_are_listed_with_runtime_args_hidden(gateway: ToolGateway) -> None:
    schema = gateway.tools["crm.issue_refund"].model_schema
    assert "approval_id" not in schema["properties"]
    assert "idempotency_key" not in schema["properties"]
    assert gateway.tools["crm.get_order"].read_only
    assert not gateway.tools["crm.issue_refund"].read_only


async def test_large_refund_without_approval_is_refused(gateway: ToolGateway) -> None:
    result = await _refund(gateway, None)
    assert not result.ok and "approval" in result.text  # type: ignore[attr-defined]


async def test_large_refund_with_approval_succeeds_once(gateway: ToolGateway, db: Database) -> None:
    args = {"order_id": "O-5008", "amount_cents": 15000, "reason": "defective"}
    approval_id = await _approval(db, "crm.issue_refund", args)
    for attempt in range(2):
        result = await gateway.call(
            "crm.issue_refund",
            args,
            runtime_args={"approval_id": approval_id, "idempotency_key": f"key-{attempt:08d}"},
        )
        if attempt == 0:
            assert result.ok
        else:  # a new call (new idempotency key) cannot reuse the consumed approval
            assert not result.ok and "already used" in result.text
    assert await db.fetchval("SELECT count(*) FROM refunds WHERE order_id = 'O-5008'") == 1


@pytest.mark.parametrize("status", ["pending", "rejected"])
async def test_undecided_or_rejected_approval_is_refused(
    gateway: ToolGateway, db: Database, status: str
) -> None:
    approval_id = await _approval(db, "crm.issue_refund", REFUND, status)
    assert not (await _refund(gateway, approval_id)).ok  # type: ignore[attr-defined]


async def test_approval_for_different_arguments_is_refused(
    gateway: ToolGateway, db: Database
) -> None:
    approval_id = await _approval(db, "crm.issue_refund", {**REFUND, "amount_cents": 15000})
    result = await _refund(gateway, approval_id)
    assert not result.ok and "does not match" in result.text  # type: ignore[attr-defined]


async def test_refund_is_idempotent(gateway: ToolGateway, db: Database) -> None:
    args = {"order_id": "O-5001", "amount_cents": 1000, "reason": "broken"}
    for _ in range(2):
        r = await gateway.call(
            "crm.issue_refund", args, runtime_args={"idempotency_key": "k-12345678"}
        )
        assert r.ok
    assert await db.fetchval("SELECT count(*) FROM refunds WHERE order_id = 'O-5001'") == 1


async def test_refund_cannot_exceed_refundable_balance(gateway: ToolGateway) -> None:
    args = {"order_id": "O-5006", "amount_cents": 100, "reason": "again"}
    r = await gateway.call("crm.issue_refund", args, runtime_args={"idempotency_key": "k-abcdefgh"})
    assert not r.ok and "exceeds refundable" in r.text


async def test_model_cannot_smuggle_runtime_args(gateway: ToolGateway, db: Database) -> None:
    approval_id = await _approval(db, "crm.issue_refund", REFUND)
    result = await gateway.call(
        "crm.issue_refund",
        {**REFUND, "approval_id": approval_id},  # model-supplied: must be stripped
        runtime_args={"idempotency_key": "k-smuggle1"},
    )
    assert not result.ok


async def test_reply_goes_only_to_requester_and_needs_approval(
    gateway: ToolGateway, db: Database
) -> None:
    args = {"ticket_id": "T-1002", "body": "Your order ships soon."}
    assert not (await gateway.call("ticketing.send_reply", args, runtime_args={})).ok
    approval_id = await _approval(db, "ticketing.send_reply", args)
    sent = await gateway.call(
        "ticketing.send_reply", args, runtime_args={"approval_id": approval_id}
    )
    assert sent.ok and sent.data["to"] == "bob@example.com"


async def test_customer_phone_numbers_are_masked(gateway: ToolGateway) -> None:
    r = await gateway.call("crm.get_customer", {"customer_id": "C-001"}, runtime_args={})
    assert r.data["phone"] == "***0101"


async def test_schema_validation_rejects_bad_ids(gateway: ToolGateway) -> None:
    r = await gateway.call("crm.get_order", {"order_id": "1; DROP TABLE orders"}, runtime_args={})
    assert not r.ok
