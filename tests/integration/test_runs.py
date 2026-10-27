"""End-to-end runs through the graph, policy, approvals and MCP servers."""

import uuid
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from opscopilot.config import Settings
from opscopilot.db.pool import Database
from opscopilot.llm.base import LLMResponse, Message, ToolCall, ToolSpec, Usage
from opscopilot.llm.scripted import ScriptedLLM
from opscopilot.runtime.gateway import ToolGateway
from opscopilot.runtime.service import ApprovalConflictError, RunService

pytestmark = pytest.mark.integration

_exporter = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(_exporter))
trace.set_tracer_provider(_provider)


async def _pending(db: Database, run_id: str) -> list[dict[str, Any]]:
    return await db.fetch(
        "SELECT id::text, tool, args FROM approvals WHERE run_id = $1::uuid AND status = 'pending'",
        run_id,
    )


async def _status(db: Database, run_id: str) -> str:
    return str(await db.fetchval("SELECT status FROM runs WHERE id = $1::uuid", run_id))


async def test_small_refund_runs_to_completion_with_reply_review(
    db: Database, gateway: ToolGateway, settings: Settings
) -> None:
    svc = RunService(db, gateway, ScriptedLLM(), settings)
    run_id = await svc.start("T-1001")
    assert await _status(db, run_id) == "awaiting_approval"
    (approval,) = await _pending(db, run_id)
    assert approval["tool"] == "ticketing.send_reply"  # the refund itself was auto-approved
    await svc.decide(approval["id"], approve=True, reviewer="t", note=None)
    assert await svc.advance(run_id) == "completed"
    assert await db.fetchval("SELECT status FROM tickets WHERE id = 'T-1001'") == "resolved"
    assert (
        await db.fetchval("SELECT sum(amount_cents) FROM refunds WHERE order_id = 'O-5001'") == 2400
    )


async def test_run_resumes_in_a_fresh_service_after_restart(
    db: Database, gateway: ToolGateway, settings: Settings
) -> None:
    run_id = await RunService(db, gateway, ScriptedLLM(), settings).start("T-1003")
    (approval,) = await _pending(db, run_id)
    assert approval["tool"] == "crm.issue_refund"

    restarted = RunService(db, gateway, ScriptedLLM(), settings)  # nothing in memory
    await restarted.decide(approval["id"], approve=True, reviewer="t", note=None)
    await restarted.advance(run_id)
    (reply,) = await _pending(db, run_id)
    assert reply["tool"] == "ticketing.send_reply"
    await restarted.decide(reply["id"], approve=True, reviewer="t", note=None)
    assert await restarted.advance(run_id) == "completed"


async def test_rejected_refund_leads_to_escalation(
    db: Database, gateway: ToolGateway, settings: Settings
) -> None:
    svc = RunService(db, gateway, ScriptedLLM(), settings)
    run_id = await svc.start("T-1015")
    (approval,) = await _pending(db, run_id)
    await svc.decide(approval["id"], approve=False, reviewer="t", note="needs photos")
    assert await svc.advance(run_id) == "completed"
    assert await db.fetchval("SELECT status FROM tickets WHERE id = 'T-1015'") == "escalated"
    assert await db.fetchval("SELECT count(*) FROM refunds WHERE order_id = 'O-5012'") == 0
    with pytest.raises(ApprovalConflictError):
        await svc.decide(approval["id"], approve=True, reviewer="t", note=None)


async def test_hijacked_model_cannot_move_money_or_leak_data(
    db: Database, gateway: ToolGateway, settings: Settings
) -> None:
    svc = RunService(db, gateway, ScriptedLLM(hijacked=True), settings)
    run_id = await svc.start("T-1008")
    assert await _status(db, run_id) == "completed"
    assert await db.fetchval("SELECT count(*) FROM refunds WHERE order_id = 'O-5007'") == 0
    assert await db.fetchval("SELECT count(*) FROM ticket_messages WHERE kind = 'reply'") == 0
    denials = await db.fetch(
        "SELECT payload->>'rule' AS rule FROM run_events WHERE run_id = $1::uuid"
        " AND kind = 'policy' AND payload->>'verdict' = 'deny'",
        run_id,
    )
    assert {d["rule"] for d in denials} == {"refund-balance", "output-validation"}
    assert (
        await db.fetchval("SELECT state->>'tainted' FROM runs WHERE id = $1::uuid", run_id)
    ) == "true"


class _Looping:
    """A model that never finishes: the budget must stop it."""

    provider, model = "test", "looping"

    async def chat(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        call = ToolCall(id=uuid.uuid4().hex, name="ticketing__list_ticket_messages",
                        arguments={"ticket_id": "T-1002"})  # fmt: skip
        return LLMResponse(
            message=Message(role="assistant", tool_calls=[call]),
            usage=Usage(input_tokens=100, output_tokens=10),
            model="looping",
        )


async def test_budget_stops_runaway_agent(
    db: Database, gateway: ToolGateway, settings: Settings
) -> None:
    tight = settings.model_copy(update={"max_llm_calls": 4})
    run_id = await RunService(db, gateway, _Looping(), tight).start("T-1002")
    assert await _status(db, run_id) == "budget_exceeded"


async def test_one_trace_per_run_with_genai_attributes(
    db: Database, gateway: ToolGateway, settings: Settings
) -> None:
    _exporter.clear()
    svc = RunService(db, gateway, ScriptedLLM(), settings)
    run_id = await svc.start("T-1003")
    (approval,) = await _pending(db, run_id)
    await svc.decide(approval["id"], approve=True, reviewer="t", note=None)
    await svc.advance(run_id)
    # The MCP SDK emits its own spans too; ours are the ones from the "opscopilot" tracer.
    spans = [
        s for s in _exporter.get_finished_spans()
        if s.instrumentation_scope is not None and s.instrumentation_scope.name == "opscopilot"
    ]  # fmt: skip
    assert {s.context.trace_id for s in spans} == {uuid.UUID(run_id).int}
    names = {s.name for s in spans}
    assert {"run_segment", "invoke_agent triage", "execute_tool crm.issue_refund"} <= names
    chat = next(s for s in spans if s.name.startswith("chat "))
    assert chat.attributes is not None and chat.attributes["gen_ai.usage.input_tokens"] > 0
