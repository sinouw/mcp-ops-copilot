"""Deterministic tool-call policy: scopes, ownership checks, money limits, output checks.

Runs between the model and every MCP call. It reads only trusted state: the ticket's
metadata from the ticketing system and facts the runtime recorded from CRM tool *results*
(never from the model's claims or from ticket text).
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

Verdict = Literal["allow", "deny", "approve"]

# Which agent may call which MCP tool. Read-only agents cannot cause side effects even if
# fully hijacked.
AGENT_SCOPES: dict[str, frozenset[str]] = {
    "triage": frozenset({"ticketing.list_ticket_messages"}),
    "research": frozenset(
        {"crm.find_customer_by_email", "crm.get_customer", "crm.list_orders", "crm.get_order"}
    ),
    "resolution": frozenset(
        {
            "crm.get_order",
            "crm.issue_refund",
            "ticketing.send_reply",
            "ticketing.add_internal_note",
            "ticketing.escalate_ticket",
            "ticketing.resolve_ticket",
        }
    ),
}
WRITE_TOOLS = frozenset(
    {
        "crm.issue_refund",
        "ticketing.send_reply",
        "ticketing.add_internal_note",
        "ticketing.escalate_ticket",
        "ticketing.resolve_ticket",
    }
)
# Writes that stay allowed on tainted runs: escalating and noting move work towards a human,
# and resolving is internal and reversible (a customer reply reopens the ticket).
SAFE_WRITES = frozenset(
    {"ticketing.escalate_ticket", "ticketing.add_internal_note", "ticketing.resolve_ticket"}
)


class Decision(BaseModel):
    verdict: Verdict
    rule: str
    reason: str


class OwnedOrder(BaseModel):
    order_id: str
    total_cents: int
    refundable_cents: int
    status: str


class TrustedFacts(BaseModel):
    """Facts derived from MCP tool results, used by policy instead of model claims."""

    ticket_id: str
    requester_email: str
    customer_id: str | None = None
    owned_orders: dict[str, OwnedOrder] = Field(default_factory=dict)
    foreign_orders: list[str] = Field(default_factory=list)
    known_emails: list[str] = Field(default_factory=list)


class PolicyConfig(BaseModel):
    approval_threshold_cents: int
    hard_cap_cents: int
    support_email: str


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?:\+?\d[\s().-]?){9,}\d")
_SECRET = re.compile(r"\b(sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,})\b")
_INTERNAL = re.compile(
    r"approval_id|idempotency_key|internal note|system prompt|untrusted-data", re.I
)


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def check_reply_body(body: str, facts: TrustedFacts, cfg: PolicyConfig) -> str | None:
    """Output guardrail for customer-facing text. Returns a violation or None."""
    allowed = {facts.requester_email.lower(), cfg.support_email.lower()}
    leaked = sorted({e.lower() for e in _EMAIL.findall(body)} - allowed)
    if leaked:
        return f"reply contains email addresses other than the requester's: {', '.join(leaked)}"
    if _PHONE.search(body):
        return "reply contains a phone number"
    if _SECRET.search(body):
        return "reply contains a credential-like string"
    if _INTERNAL.search(body):
        return "reply leaks internal identifiers or instructions"
    return None


def evaluate(
    agent: str,
    tool: str,
    args: dict[str, Any],
    facts: TrustedFacts,
    cfg: PolicyConfig,
    *,
    tainted: bool,
) -> Decision:
    if tool not in AGENT_SCOPES.get(agent, frozenset()):
        return Decision(verdict="deny", rule="scope", reason=f"{agent} may not call {tool}")

    ticket_arg = args.get("ticket_id")
    if ticket_arg is not None and ticket_arg != facts.ticket_id:
        return Decision(
            verdict="deny", rule="ticket-binding", reason="tools may only act on this run's ticket"
        )

    if tool == "crm.issue_refund":
        decision = _refund(args, facts, cfg, tainted=tainted)
        if decision.verdict == "approve" and tainted and decision.rule != "taint":
            decision.reason += " (run flagged for possible prompt injection)"
        return decision

    if tool == "ticketing.send_reply":
        violation = check_reply_body(str(args.get("body", "")), facts, cfg)
        if violation:
            return Decision(verdict="deny", rule="output-validation", reason=violation)
        return Decision(
            verdict="approve",
            rule="reply-review",
            reason=f"customer email to {facts.requester_email} needs human review",
        )

    if tainted and tool in WRITE_TOOLS and tool not in SAFE_WRITES:
        return Decision(
            verdict="approve",
            rule="taint",
            reason="ticket content looks like a prompt injection; all writes need review",
        )
    return Decision(verdict="allow", rule="default", reason="within scope")


def _refund(
    args: dict[str, Any], facts: TrustedFacts, cfg: PolicyConfig, *, tainted: bool
) -> Decision:
    order_id = str(args.get("order_id", ""))
    amount = args.get("amount_cents")
    if not isinstance(amount, int) or amount <= 0:
        return Decision(verdict="deny", rule="refund-args", reason="amount_cents must be > 0")
    if facts.customer_id is None:
        return Decision(
            verdict="deny",
            rule="refund-ownership",
            reason="requester has not been verified as a CRM customer",
        )
    order = facts.owned_orders.get(order_id)
    if order is None:
        return Decision(
            verdict="deny",
            rule="refund-ownership",
            reason=f"{order_id} is not a verified order of the ticket requester",
        )
    if amount > order.refundable_cents:
        return Decision(
            verdict="deny",
            rule="refund-balance",
            reason=f"{_money(amount)} exceeds refundable balance {_money(order.refundable_cents)}",
        )
    if amount > cfg.hard_cap_cents:
        return Decision(
            verdict="deny",
            rule="refund-hard-cap",
            reason=f"{_money(amount)} is above the {_money(cfg.hard_cap_cents)} automation cap;"
            " escalate to a human",
        )
    if amount > cfg.approval_threshold_cents:
        return Decision(
            verdict="approve",
            rule="refund-threshold",
            reason=f"refund {_money(amount)} on {order_id} is above the"
            f" {_money(cfg.approval_threshold_cents)} auto-approve threshold",
        )
    if tainted:
        return Decision(
            verdict="approve",
            rule="taint",
            reason="ticket content looks like a prompt injection; refund needs review",
        )
    return Decision(verdict="allow", rule="refund-auto", reason="small refund on a verified order")


def record_tool_result(facts: TrustedFacts, tool: str, result: dict[str, Any]) -> None:
    """Update trusted facts from structured MCP results (not from free text)."""
    if tool in ("crm.find_customer_by_email", "crm.get_customer"):
        email = str(result.get("email", "")).lower()
        if email:
            facts.known_emails = sorted(set(facts.known_emails) | {email})
        if email == facts.requester_email.lower():
            facts.customer_id = str(result["id"])
    elif tool == "crm.list_orders":
        for o in result.get("orders", []):
            _record_order(facts, o)
    elif tool == "crm.get_order":
        _record_order(facts, result)
    elif tool == "crm.issue_refund" and result.get("status") == "refunded":
        owned = facts.owned_orders.get(str(result.get("order_id")))
        if owned is not None:
            owned.refundable_cents = int(result["remaining_refundable_cents"])


def _record_order(facts: TrustedFacts, o: dict[str, Any]) -> None:
    oid = str(o.get("id"))
    if facts.customer_id is not None and o.get("customer_id") == facts.customer_id:
        facts.owned_orders[oid] = OwnedOrder(
            order_id=oid,
            total_cents=int(o["total_cents"]),
            refundable_cents=int(o["refundable_cents"]),
            status=str(o["status"]),
        )
    elif oid not in facts.foreign_orders:
        facts.foreign_orders.append(oid)
