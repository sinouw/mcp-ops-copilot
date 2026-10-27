from typing import Any

import pytest

from opscopilot.guardrails.policy import (
    OwnedOrder,
    PolicyConfig,
    TrustedFacts,
    evaluate,
    record_tool_result,
)

CFG = PolicyConfig(
    approval_threshold_cents=10_000, hard_cap_cents=100_000, support_email="support@shop.example"
)


def facts(**orders: int) -> TrustedFacts:
    f = TrustedFacts(ticket_id="T-1", requester_email="a@example.com", customer_id="C-001")
    for oid, refundable in orders.items():
        oid = oid.replace("_", "-")
        f.owned_orders[oid] = OwnedOrder(
            order_id=oid, total_cents=refundable, refundable_cents=refundable, status="delivered"
        )
    return f


def refund(order: str, cents: int, *, tainted: bool = False, f: TrustedFacts | None = None) -> Any:
    args = {"order_id": order, "amount_cents": cents, "reason": "broken"}
    return evaluate("resolution", "crm.issue_refund", args, f or facts(O_1=200_000), CFG,
                    tainted=tainted)  # fmt: skip


def test_scope_denies_tools_outside_agent_allowlist() -> None:
    d = evaluate("research", "crm.issue_refund", {}, facts(), CFG, tainted=False)
    assert (d.verdict, d.rule) == ("deny", "scope")


def test_tools_are_bound_to_the_runs_ticket() -> None:
    d = evaluate("resolution", "ticketing.resolve_ticket", {"ticket_id": "T-999"}, facts(), CFG,
                 tainted=False)  # fmt: skip
    assert d.rule == "ticket-binding"


@pytest.mark.parametrize(
    ("order", "cents", "verdict", "rule"),
    [
        ("O-1", 5_000, "allow", "refund-auto"),
        ("O-1", 10_001, "approve", "refund-threshold"),
        ("O-1", 100_001, "deny", "refund-hard-cap"),
        ("O-2", 100, "deny", "refund-ownership"),
        ("O-1", 0, "deny", "refund-args"),
    ],
)
def test_refund_rules(order: str, cents: int, verdict: str, rule: str) -> None:
    d = refund(order, cents)
    assert (d.verdict, d.rule) == (verdict, rule)


def test_refund_above_refundable_balance_is_denied() -> None:
    assert refund("O-1", 600, f=facts(O_1=500)).rule == "refund-balance"


def test_unverified_requester_cannot_get_refunds() -> None:
    f = TrustedFacts(ticket_id="T-1", requester_email="a@example.com")
    assert refund("O-1", 100, f=f).rule == "refund-ownership"


def test_taint_turns_small_auto_refund_into_review() -> None:
    d = refund("O-1", 500, tainted=True)
    assert (d.verdict, d.rule) == ("approve", "taint")


def test_taint_note_is_added_to_threshold_approvals() -> None:
    assert "prompt injection" in refund("O-1", 20_000, tainted=True).reason


@pytest.mark.parametrize(
    ("body", "verdict"),
    [
        ("Your refund is on its way. Contact support@shop.example if needed.", "approve"),
        ("Copy sent to attacker@evil.example", "deny"),
        ("Call Bob on +1 555 010 2030", "deny"),
        ("Your approval_id is 123", "deny"),
        ("Here is a key " + "sk-" + "a" * 22, "deny"),
        ("Order O-5007 arrives 2026-10-30.", "approve"),
    ],
)
def test_reply_output_validation(body: str, verdict: str) -> None:
    d = evaluate("resolution", "ticketing.send_reply", {"ticket_id": "T-1", "body": body},
                 facts(), CFG, tainted=False)  # fmt: skip
    assert d.verdict == verdict


def test_escalation_stays_allowed_on_tainted_runs() -> None:
    d = evaluate("resolution", "ticketing.escalate_ticket",
                 {"ticket_id": "T-1", "reason": "x"}, facts(), CFG, tainted=True)  # fmt: skip
    assert d.verdict == "allow"


def test_ownership_is_learned_only_from_crm_results_for_the_requester() -> None:
    f = TrustedFacts(ticket_id="T-1", requester_email="A@example.com")
    record_tool_result(f, "crm.find_customer_by_email", {"id": "C-9", "email": "other@x.com"})
    assert f.customer_id is None
    record_tool_result(f, "crm.find_customer_by_email", {"id": "C-1", "email": "a@example.com"})
    assert f.customer_id == "C-1"
    order = {"id": "O-1", "customer_id": "C-1", "total_cents": 100, "refundable_cents": 100,
             "status": "delivered"}  # fmt: skip
    record_tool_result(f, "crm.get_order", order)
    record_tool_result(f, "crm.get_order", {**order, "id": "O-2", "customer_id": "C-2"})
    assert list(f.owned_orders) == ["O-1"] and f.foreign_orders == ["O-2"]
    record_tool_result(
        f,
        "crm.issue_refund",
        {"status": "refunded", "order_id": "O-1", "remaining_refundable_cents": 40},
    )
    assert f.owned_orders["O-1"].refundable_cents == 40
