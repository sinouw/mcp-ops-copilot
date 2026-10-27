"""Provider-agnostic chat + tool-calling types. Every provider adapter speaks these."""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None


class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    estimated: bool = False


class LLMResponse(BaseModel):
    message: Message
    usage: Usage
    model: str


class LLMError(RuntimeError):
    pass


class LLMClient(Protocol):
    provider: str
    model: str

    async def chat(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse: ...


def cost_usd(usage: Usage, input_per_mtok: float, output_per_mtok: float) -> float:
    return (usage.input_tokens * input_per_mtok + usage.output_tokens * output_per_mtok) / 1e6
