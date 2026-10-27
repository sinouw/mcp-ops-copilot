import json
from typing import Any

import httpx
import pytest

from opscopilot.agents.graph import Goto, Graph, GraphError, Pause
from opscopilot.agents.specialist import inline_refs
from opscopilot.agents.state import END, RunState, TicketView
from opscopilot.guardrails.budget import BudgetExceededError, BudgetLimits, BudgetUsage
from opscopilot.guardrails.policy import TrustedFacts
from opscopilot.llm.base import LLMError, Message, ToolSpec
from opscopilot.llm.openai_compat import OpenAICompatibleClient


def _state() -> RunState:
    t = TicketView(id="T-1", customer_email="a@x.com", subject="s", body="b")
    return RunState(
        run_id="r", ticket=t, facts=TrustedFacts(ticket_id="T-1", requester_email="a@x.com")
    )


async def test_graph_runs_to_end_and_checkpoints_each_step() -> None:
    seen: list[str] = []

    async def a(state: RunState, ctx: None) -> Goto:
        return Goto("b")

    async def b(state: RunState, ctx: None) -> Goto:
        return Goto(END)

    async def cp(state: RunState) -> None:
        seen.append(state.node)

    state = _state()
    state.node = "a"
    g: Graph[None] = Graph({"a": a, "b": b}, {"a": ["b"], "b": [END]})
    assert await g.run(state, None, cp) == "completed"
    assert seen == ["b", END]


async def test_graph_rejects_undeclared_transitions() -> None:
    async def a(state: RunState, ctx: None) -> Goto:
        return Goto(END)

    state = _state()
    state.node = "a"
    g: Graph[None] = Graph({"a": a}, {"a": []})

    async def cp(_: RunState) -> None: ...

    with pytest.raises(GraphError):
        await g.run(state, None, cp)


async def test_graph_pause_keeps_node_for_resume() -> None:
    async def a(state: RunState, ctx: None) -> Pause:
        return Pause("ap-1")

    state = _state()
    state.node = "a"

    async def cp(_: RunState) -> None: ...

    assert await Graph({"a": a}, {"a": []}).run(state, None, cp) == "paused"
    assert state.node == "a"


def test_graph_validates_edges_at_build_time() -> None:
    with pytest.raises(GraphError):
        Graph({}, {"a": ["b"]})


def test_budget_limits() -> None:
    limits = BudgetLimits(max_llm_calls=2, max_tool_calls=5, max_total_tokens=100, max_cost_usd=1)
    BudgetUsage(llm_calls=1).check(limits)
    for usage in (BudgetUsage(llm_calls=2), BudgetUsage(tokens_in=60, tokens_out=40),
                  BudgetUsage(cost_usd=1.0), BudgetUsage(tool_calls=5)):  # fmt: skip
        with pytest.raises(BudgetExceededError):
            usage.check(limits)


def test_inline_refs_removes_defs() -> None:
    schema = {"$defs": {"X": {"type": "integer", "title": "X"}},
              "properties": {"x": {"$ref": "#/$defs/X"}}}  # fmt: skip
    assert inline_refs(schema) == {"properties": {"x": {"type": "integer"}}}


def _client(handler: Any) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        provider="groq", model="m", api_key="test", transport=httpx.MockTransport(handler),
        max_retries=2,
    )  # fmt: skip


async def test_openai_compat_parses_tool_calls_and_usage() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert body["tools"][0]["function"]["name"] == "lookup"
        assert req.headers["authorization"] == "Bearer test"
        return httpx.Response(200, json={
            "model": "m-1",
            "choices": [{"message": {"content": None, "tool_calls": [
                {"id": "c1", "function": {"name": "lookup", "arguments": '{"q": 1}'}}]}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 3},
        })  # fmt: skip

    resp = await _client(handler).chat(
        [Message(role="user", content="hi")],
        [ToolSpec(name="lookup", description="", parameters={})],
    )
    assert resp.message.tool_calls[0].arguments == {"q": 1}
    assert (resp.usage.input_tokens, resp.usage.output_tokens, resp.model) == (11, 3, "m-1")


async def test_openai_compat_retries_rate_limits() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    resp = await _client(handler).chat([Message(role="user", content="hi")], [])
    assert resp.message.content == "ok" and calls["n"] == 2


async def test_openai_compat_does_not_retry_client_errors() -> None:
    with pytest.raises(LLMError, match="401"):
        await _client(lambda r: httpx.Response(401, text="bad key")).chat([], [])


async def test_openai_compat_flags_invalid_argument_json() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [
            {"id": "c", "function": {"name": "f", "arguments": "{oops"}}]}}]})  # fmt: skip

    resp = await _client(handler).chat([], [])
    assert "__invalid_json__" in resp.message.tool_calls[0].arguments
