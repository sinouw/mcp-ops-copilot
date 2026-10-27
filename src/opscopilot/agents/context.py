"""What nodes get to work with. Implemented by runtime.service.RunService."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from opscopilot.guardrails.budget import BudgetLimits
from opscopilot.guardrails.policy import PolicyConfig
from opscopilot.llm.base import LLMClient
from opscopilot.runtime.gateway import ToolGateway


class EventSink(Protocol):
    async def emit(
        self,
        kind: str,
        agent: str | None,
        payload: dict[str, Any],
        duration_ms: float | None = None,
    ) -> None: ...


class ApprovalStore(Protocol):
    async def create(self, agent: str, tool: str, args: dict[str, Any], reason: str) -> str: ...

    async def get(self, approval_id: str) -> dict[str, Any]: ...


@dataclass
class AgentContext:
    llm: LLMClient
    gateway: ToolGateway
    policy: PolicyConfig
    limits: BudgetLimits
    price_in_per_mtok: float
    price_out_per_mtok: float
    events: EventSink
    approvals: ApprovalStore
    max_ticket_chars: int = 4000
