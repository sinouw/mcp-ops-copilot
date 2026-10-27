"""OpenTelemetry setup. Span and attribute names follow the GenAI semantic conventions."""

from __future__ import annotations

import secrets
import uuid

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
)
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

from opscopilot.config import Settings

_configured = False


def setup_tracing(settings: Settings, extra_exporter: SpanExporter | None = None) -> None:
    global _configured
    if _configured:
        return
    provider = TracerProvider(
        resource=Resource.create({"service.name": settings.otel_service_name})
    )
    if settings.otel_exporter_otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        endpoint = settings.otel_exporter_otlp_endpoint.rstrip("/") + "/v1/traces"
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    if settings.otel_console:
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    if extra_exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(extra_exporter))
    trace.set_tracer_provider(provider)
    _configured = True


tracer = trace.get_tracer("opscopilot")


def run_trace_context(run_id: uuid.UUID) -> Context:
    """Parent context whose trace id *is* the run id.

    A run that pauses for approval resumes later in a different task; deriving the trace id
    from the run id puts every segment of the run into one trace without storing span state.
    """
    parent = SpanContext(
        trace_id=run_id.int,
        span_id=int.from_bytes(secrets.token_bytes(8), "big") or 1,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    return trace.set_span_in_context(NonRecordingSpan(parent))
