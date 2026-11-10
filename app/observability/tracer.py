"""Tracing: spans around the pipeline steps and every LLM call, exported over OpenTelemetry only when configured.

Off by default – the own LLM call log (app/observability/llm_calls.py) is always there. With
`OTEL_EXPORTER_OTLP_ENDPOINT` set (the standard OpenTelemetry variable; headers via `OTEL_EXPORTER_OTLP_HEADERS`),
spans and metrics go over OTLP/HTTP to the company's collector (Datadog, Grafana, Splunk …). Spans and metrics carry
names, models, durations, token counts, cost and outcomes – never prompt or answer text.
"""
from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

logger = logging.getLogger(__name__)

SERVICE_NAME = "derkurator-ai"
_lock = threading.Lock()
_status: "TracingStatus | None" = None
_metrics: dict[str, Any] = {}


@dataclass
class TracingStatus:
    enabled: bool
    endpoint: str = ""
    message: str = ""


def _endpoint() -> str:
    return (os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or "").strip()


def _host(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).netloc or url


def init_tracing() -> TracingStatus:
    """Idempotent: registers the OTLP exporter for spans and metrics when an endpoint is configured."""
    global _status
    with _lock:
        if _status is not None:
            return _status
        endpoint = _endpoint()
        if not endpoint:
            _status = TracingStatus(False, message="OpenTelemetry export off (OTEL_EXPORTER_OTLP_ENDPOINT not set)")
            return _status
        try:
            from opentelemetry import metrics, trace
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            resource = Resource.create({"service.name": os.getenv("OTEL_SERVICE_NAME", SERVICE_NAME)})
            provider = TracerProvider(resource=resource)
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))  # endpoint/headers from OTEL_* env
            trace.set_tracer_provider(provider)
            metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[
                PeriodicExportingMetricReader(OTLPMetricExporter())]))
            _status = TracingStatus(True, _host(endpoint), "OpenTelemetry export active (spans and metrics, no content)")
        except Exception as exc:
            logger.exception("OpenTelemetry export could not be initialised")
            _status = TracingStatus(False, _host(endpoint), f"OpenTelemetry export unavailable ({exc.__class__.__name__})")
        return _status


def reset_tracing() -> None:
    """Tests: forget the status, so the next init reads the environment again."""
    global _status
    with _lock:
        _status = None
        _metrics.clear()


def _set(span: Any, attributes: dict[str, Any]) -> None:
    for key, value in attributes.items():
        if value is None:
            continue
        try:
            span.set_attribute(key, value if isinstance(value, (str, int, float, bool)) else str(value))
        except Exception:  # noqa: BLE001, S110 - a tracing attribute must never fail the traced call
            pass


@contextmanager
def trace_span(name: str, **attributes: Any) -> Iterator[Any]:
    """Span around a pipeline step; a no-op without an exporter (the API's default tracer records nothing)."""
    try:
        from opentelemetry import trace

        tracer = trace.get_tracer(SERVICE_NAME)
    except Exception:  # noqa: BLE001
        yield None
        return
    with tracer.start_as_current_span(name) as span:
        _set(span, attributes)
        yield span


def _instruments() -> dict[str, Any]:
    if not _metrics:
        from opentelemetry import metrics

        meter = metrics.get_meter(SERVICE_NAME)
        _metrics.update(
            tokens=meter.create_counter("llm.tokens", unit="{token}", description="Tokens per LLM call"),
            calls=meter.create_counter("llm.calls", unit="{call}", description="LLM calls by outcome"),
            duration=meter.create_histogram("llm.duration", unit="s", description="Duration of an LLM call"),
            cost=meter.create_counter("llm.cost", unit="USD", description="Estimated cost of LLM calls"))
    return _metrics


def record_llm_call(entry: dict[str, Any]) -> None:
    """One finished LLM call (an entry of the call log) as a span and as metrics – only while the export is on."""
    status = _status
    if status is None or not status.enabled:
        return
    try:
        labels = {"caller": entry["caller"], "model": entry["model"], "provider": entry["provider"]}
        with trace_span("llm.call", **labels, kind=entry["kind"], outcome=entry["outcome"],
                        duration_s=entry["duration_s"], prompt_tokens=entry["prompt_tokens"],
                        completion_tokens=entry["completion_tokens"], cost_usd=entry["cost_usd"],
                        tool_calls=entry["tool_calls"]):
            pass
        m = _instruments()
        m["tokens"].add(entry["prompt_tokens"], {**labels, "direction": "input"})
        m["tokens"].add(entry["completion_tokens"], {**labels, "direction": "output"})
        m["calls"].add(1, {**labels, "outcome": entry["outcome"].split(":")[0]})
        m["duration"].record(entry["duration_s"], labels)
        if entry["cost_usd"] is not None:
            m["cost"].add(entry["cost_usd"], labels)
    except Exception:
        logger.debug("LLM call telemetry skipped", exc_info=True)
