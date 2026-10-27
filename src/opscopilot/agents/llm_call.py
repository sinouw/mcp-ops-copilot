"""Traced, budgeted LLM call shared by all agents."""

from __future__ import annotations

import time

from opscopilot.agents.context import AgentContext
from opscopilot.agents.state import RunState
from opscopilot.llm.base import LLMResponse, Message, ToolSpec, cost_usd
from opscopilot.telemetry import tracer


async def chat(
    state: RunState, ctx: AgentContext, agent: str, messages: list[Message], tools: list[ToolSpec]
) -> LLMResponse:
    state.usage.check(ctx.limits)
    with tracer.start_as_current_span(f"chat {ctx.llm.model}") as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.system", ctx.llm.provider)
        span.set_attribute("gen_ai.request.model", ctx.llm.model)
        span.set_attribute("gen_ai.agent.name", agent)
        t0 = time.perf_counter()
        resp = await ctx.llm.chat(messages, tools)
        ms = (time.perf_counter() - t0) * 1000
        cost = cost_usd(resp.usage, ctx.price_in_per_mtok, ctx.price_out_per_mtok)
        span.set_attribute("gen_ai.response.model", resp.model)
        span.set_attribute("gen_ai.usage.input_tokens", resp.usage.input_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", resp.usage.output_tokens)
        span.set_attribute("opscopilot.cost_usd", cost)
        span.set_attribute("opscopilot.tool_calls", [tc.name for tc in resp.message.tool_calls])
    state.usage.llm_calls += 1
    state.usage.tokens_in += resp.usage.input_tokens
    state.usage.tokens_out += resp.usage.output_tokens
    state.usage.cost_usd += cost
    await ctx.events.emit(
        "llm",
        agent,
        {
            "model": resp.model,
            "input_tokens": resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens,
            "tokens_estimated": resp.usage.estimated,
            "cost_usd": cost,
            "tool_calls": [tc.name for tc in resp.message.tool_calls],
            "content": resp.message.content[:500],
        },
        ms,
    )
    return resp
