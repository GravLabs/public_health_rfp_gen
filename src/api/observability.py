"""
Observability setup: Azure Monitor + OpenTelemetry for the Review API.
Tracks: request traces, token usage spans, gate decision events,
SharePoint/Fabric operation durations.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from azure.monitor.opentelemetry import configure_azure_monitor
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.trace import Span, Status, StatusCode

log = logging.getLogger(__name__)
tracer = trace.get_tracer("pubhealth.rfp.api")

COST_PER_1K_PROMPT = float(os.getenv("GPT4O_PROMPT_COST_PER_1K", "0.0025"))   # $2.50/1M input
COST_PER_1K_COMPLETION = float(os.getenv("GPT4O_COMPLETION_COST_PER_1K", "0.010"))  # $10/1M output


def setup_observability(app, connection_string: Optional[str] = None) -> None:
    """Configure App Insights + OpenTelemetry. Call once at startup."""
    conn_str = connection_string or os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if not conn_str:
        log.warning("APPLICATIONINSIGHTS_CONNECTION_STRING not set — telemetry disabled")
        return

    configure_azure_monitor(connection_string=conn_str)
    FastAPIInstrumentor.instrument_app(app)
    HTTPXClientInstrumentor().instrument()
    log.info("Azure Monitor observability configured")


def record_generation_span(
    span: Span,
    draft_id: str,
    rfp_id: str,
    program_area: str,
    prompt_tokens: int,
    completion_tokens: int,
    gate_decision: Optional[str] = None,
) -> None:
    """Record all generation telemetry on a span."""
    estimated_cost = (
        (prompt_tokens / 1000) * COST_PER_1K_PROMPT
        + (completion_tokens / 1000) * COST_PER_1K_COMPLETION
    )

    span.set_attribute("pubhealth.draft_id", draft_id)
    span.set_attribute("pubhealth.rfp_id", rfp_id)
    span.set_attribute("pubhealth.program_area", program_area)
    span.set_attribute("llm.prompt_tokens", prompt_tokens)
    span.set_attribute("llm.completion_tokens", completion_tokens)
    span.set_attribute("llm.total_tokens", prompt_tokens + completion_tokens)
    span.set_attribute("llm.estimated_cost_usd", round(estimated_cost, 6))
    if gate_decision:
        span.set_attribute("pubhealth.gate_decision", gate_decision)


def record_evaluation_span(span: Span, scores: dict[str, float], failure_reasons: list[str]) -> None:
    """Record evaluator scores on a span."""
    for name, score in scores.items():
        span.set_attribute(f"eval.{name}", round(score, 4))
    span.set_attribute("eval.failure_count", len(failure_reasons))
    if failure_reasons:
        span.set_attribute("eval.failures", "; ".join(failure_reasons[:3]))


def record_sharepoint_span(span: Span, operation: str, library: str, success: bool, url: Optional[str] = None) -> None:
    span.set_attribute("sharepoint.operation", operation)
    span.set_attribute("sharepoint.library", library)
    span.set_attribute("sharepoint.success", success)
    if url:
        span.set_attribute("sharepoint.result_url", url)
    if not success:
        span.set_status(Status(StatusCode.ERROR))


def record_fabric_span(span: Span, operation: str, path: Optional[str] = None, success: bool = True) -> None:
    span.set_attribute("fabric.operation", operation)
    span.set_attribute("fabric.success", success)
    if path:
        span.set_attribute("fabric.path", path)
    if not success:
        span.set_status(Status(StatusCode.ERROR))
