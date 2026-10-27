"""Per-run cost and step budgets. Exceeding any limit stops the run, it never degrades."""

from __future__ import annotations

from pydantic import BaseModel


class BudgetExceededError(RuntimeError):
    pass


class BudgetLimits(BaseModel):
    max_llm_calls: int
    max_tool_calls: int
    max_total_tokens: int
    max_cost_usd: float


class BudgetUsage(BaseModel):
    llm_calls: int = 0
    tool_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0

    def check(self, limits: BudgetLimits) -> None:
        if self.llm_calls >= limits.max_llm_calls:
            raise BudgetExceededError(f"LLM call budget of {limits.max_llm_calls} reached")
        if self.tool_calls >= limits.max_tool_calls:
            raise BudgetExceededError(f"tool call budget of {limits.max_tool_calls} reached")
        if self.tokens_in + self.tokens_out >= limits.max_total_tokens:
            raise BudgetExceededError(f"token budget of {limits.max_total_tokens} reached")
        if self.cost_usd >= limits.max_cost_usd:
            raise BudgetExceededError(f"cost budget of ${limits.max_cost_usd:.2f} reached")
