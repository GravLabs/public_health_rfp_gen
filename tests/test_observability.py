"""Unit tests for observability helpers — validates span attributes are set correctly."""
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "api"))
import observability as obs


def _make_span():
    span = MagicMock()
    span._attrs = {}
    span.set_attribute = lambda k, v: span._attrs.update({k: v})
    return span


def test_record_generation_span_sets_token_attributes():
    span = _make_span()
    obs.record_generation_span(
        span=span,
        draft_id="abc123",
        rfp_id="Public Health Labs-RFP-2024-TEST",
        program_area="Public Health Emergency Preparedness",
        prompt_tokens=5000,
        completion_tokens=1200,
        gate_decision="PASS",
    )
    assert span._attrs["pubhealth.draft_id"] == "abc123"
    assert span._attrs["llm.prompt_tokens"] == 5000
    assert span._attrs["llm.completion_tokens"] == 1200
    assert span._attrs["llm.total_tokens"] == 6200
    assert span._attrs["llm.estimated_cost_usd"] > 0
    assert span._attrs["pubhealth.gate_decision"] == "PASS"


def test_record_generation_span_calculates_cost():
    span = _make_span()
    obs.record_generation_span(span, "d1", "r1", "Test", 10_000, 2_000)
    expected = (10 * obs.COST_PER_1K_PROMPT) + (2 * obs.COST_PER_1K_COMPLETION)
    assert abs(span._attrs["llm.estimated_cost_usd"] - expected) < 1e-9


def test_record_evaluation_span_sets_scores():
    span = _make_span()
    obs.record_evaluation_span(
        span=span,
        scores={"completeness": 0.95, "groundedness": 0.88, "compliance": 1.0},
        failure_reasons=[]
    )
    assert span._attrs["eval.completeness"] == 0.95
    assert span._attrs["eval.groundedness"] == 0.88
    assert span._attrs["eval.failure_count"] == 0


def test_record_evaluation_span_records_failures():
    span = _make_span()
    obs.record_evaluation_span(
        span=span,
        scores={"completeness": 0.4},
        failure_reasons=["completeness: background too short", "compliance: prohibited pattern"]
    )
    assert span._attrs["eval.failure_count"] == 2
    assert "completeness" in span._attrs.get("eval.failures", "")


def test_record_sharepoint_span():
    span = _make_span()
    obs.record_sharepoint_span(span, "upload", "Generated Drafts", True, "https://sp/file.md")
    assert span._attrs["sharepoint.operation"] == "upload"
    assert span._attrs["sharepoint.success"] is True
    assert span._attrs["sharepoint.result_url"] == "https://sp/file.md"


def test_record_fabric_span_failure_sets_error_status():
    from unittest.mock import patch, MagicMock
    span = MagicMock()
    span._attrs = {}
    span.set_attribute = lambda k, v: span._attrs.update({k: v})
    obs.record_fabric_span(span, "write_eval_record", success=False)
    assert span._attrs["fabric.success"] is False
    span.set_status.assert_called_once()
