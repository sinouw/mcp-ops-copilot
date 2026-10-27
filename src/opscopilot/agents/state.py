"""Durable run state. Everything needed to resume a paused run lives in this JSON document."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from opscopilot.guardrails.budget import BudgetUsage
from opscopilot.guardrails.policy import TrustedFacts
from opscopilot.llm.base import Message, ToolCall

END = "end"


class AgentSession(BaseModel):
    messages: list[Message] = Field(default_factory=list)
    # Tool calls from the last assistant turn that have not been answered yet.
    queue: list[ToolCall] = Field(default_factory=list)
    awaiting_approval: str | None = None
    validation_retries: int = 0


class TicketView(BaseModel):
    """Ticket as loaded by the runtime. `subject`/`body` are untrusted customer text."""

    id: str
    customer_email: str
    subject: str
    body: str


class InjectionFinding(BaseModel):
    source: str
    score: float
    signals: list[str]


class RunState(BaseModel):
    run_id: str
    ticket: TicketView
    node: str = "supervisor"
    path: list[str] = Field(default_factory=list)
    visits: dict[str, int] = Field(default_factory=dict)
    findings: dict[str, dict[str, Any]] = Field(default_factory=dict)
    sessions: dict[str, AgentSession] = Field(default_factory=dict)
    facts: TrustedFacts
    tainted: bool = False
    injections: list[InjectionFinding] = Field(default_factory=list)
    usage: BudgetUsage = Field(default_factory=BudgetUsage)
    status: Literal["running", "awaiting_approval", "completed", "failed", "budget_exceeded"] = (
        "running"
    )
    error: str | None = None
