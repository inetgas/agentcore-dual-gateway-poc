"""OTel wiring → a single OTLP egress (the OTel Collector).

A BaggageSpanProcessor copies conversation_id/user_id/session.id onto every span,
plus an OTLP exporter pointed at OTEL_EXPORTER_OTLP_ENDPOINT (the Collector, which fans
out to Jaeger raw + Langfuse redacted). Enabled only when that env is set.
"""

from __future__ import annotations

import os

from opentelemetry import baggage, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

_KEYS = ("conversation_id", "user_id", "session.id")


class BaggageSpanProcessor(SpanProcessor):
    def on_start(self, span, parent_context=None):
        for k in _KEYS:
            v = baggage.get_baggage(k, context=parent_context)
            if v is not None:
                span.set_attribute(k, v)

    def on_end(self, span): pass
    def shutdown(self): pass
    def force_flush(self, timeout_millis: int = 30000) -> bool: return True


def setup() -> bool:
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return False
    provider = TracerProvider(resource=Resource.create(
        {"service.name": os.environ.get("OTEL_SERVICE_NAME", "mvp-orchestrator")}))
    provider.add_span_processor(BaggageSpanProcessor())
    # Demo: SimpleSpanProcessor exports each span immediately so traces show up
    # in Jaeger the instant you send a chat turn. Prod uses BatchSpanProcessor.
    # Single OTLP egress → the OTel Collector, which fans out to the backends (Jaeger raw,
    # Langfuse PII-redacted). The runtime no longer talks to Langfuse directly — redaction
    # and routing live in the Collector (Decision 4 / Pattern B).
    exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
    if os.environ.get("OTEL_BATCH", "").lower() in ("1", "true"):
        provider.add_span_processor(BatchSpanProcessor(exporter))
    else:
        provider.add_span_processor(SimpleSpanProcessor(exporter))

    trace.set_tracer_provider(provider)
    return True


def tracer():
    return trace.get_tracer("orchestrator")
