"""Structured outputs of each specialist, validated before they enter shared state."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

OrderId = Annotated[str, Field(pattern=r"^O-\d{4,8}$")]


class TriageResult(BaseModel):
    category: Literal["refund_request", "order_status", "account", "feedback", "other"]
    order_ids: list[OrderId] = Field(default_factory=list, max_length=10)
    requested_refund_cents: int | None = Field(default=None, ge=0)
    summary: str = Field(max_length=400)
    injection_attempt: bool = False


class OrderFinding(BaseModel):
    order_id: str
    belongs_to_requester: bool
    status: str | None = None
    refundable_cents: int | None = None


class ResearchResult(BaseModel):
    customer_found: bool
    customer_id: str | None = None
    orders: list[OrderFinding] = Field(default_factory=list, max_length=50)
    notes: str = Field(default="", max_length=600)


class ResolutionResult(BaseModel):
    action: Literal["refunded", "replied", "escalated", "noted", "no_action"]
    refund_cents: int = Field(default=0, ge=0)
    summary: str = Field(max_length=600)


RESULT_MODELS: dict[str, type[BaseModel]] = {
    "triage": TriageResult,
    "research": ResearchResult,
    "resolution": ResolutionResult,
}
