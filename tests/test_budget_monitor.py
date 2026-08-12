"""Unit tests for BudgetMonitor and SessionCostTracker."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "api"))
import budget_monitor as bm


@pytest.fixture(autouse=True)
def reset_tracker():
    """Reset the session tracker between tests."""
    bm.session_tracker.prompt_tokens = 0
    bm.session_tracker.completion_tokens = 0
    bm.session_tracker.embedding_tokens = 0
    bm.session_tracker.request_count = 0
    bm.session_tracker._cost_usd = 0.0
    yield


def test_session_tracker_accumulates_correctly():
    tracker = bm.SessionCostTracker()
    cost1 = tracker.record_generation(1000, 200)
    cost2 = tracker.record_generation(500, 100)

    assert tracker.prompt_tokens == 1500
    assert tracker.completion_tokens == 300
    assert tracker.request_count == 2
    expected_cost = (1.5 * bm.GPT4O_PROMPT_RATE) + (0.3 * bm.GPT4O_COMPLETION_RATE)
    assert abs(tracker.total_cost_usd - expected_cost) < 1e-9


def test_session_tracker_embedding_cost():
    tracker = bm.SessionCostTracker()
    cost = tracker.record_embedding(10_000)
    expected = (10 * bm.EMBEDDING_RATE)
    assert abs(cost - expected) < 1e-9
    assert tracker.total_tokens == 10_000


def test_estimate_request_cost_structure():
    bm.session_tracker.record_generation(500, 100)
    result = bm.BudgetMonitor.estimate_request_cost(1000, 200)
    assert "request_cost_usd" in result
    assert "session_cost_usd" in result
    assert "session_request_count" in result
    assert "estimated_requests_remaining_in_budget" in result
    assert result["request_cost_usd"] > 0
    assert result["estimated_requests_remaining_in_budget"] > 0


def test_budget_alert_levels():
    # Mock a monitor and test the alert level logic inline
    with patch("budget_monitor.DefaultAzureCredential"), \
         patch("budget_monitor.get_bearer_token_provider", return_value=lambda _: lambda: "tok"):
        monitor = bm.BudgetMonitor("sub-123", "rg-test")

    # Test logic: actual=420/500 → 84% → warn
    actual = 420.0
    budget = 500.0
    pct = actual / budget
    assert pct >= bm.WARN_THRESHOLD
    assert pct < bm.CRITICAL_THRESHOLD

    # 490/500 → 98% → critical
    pct_crit = 490.0 / 500.0
    assert pct_crit >= bm.CRITICAL_THRESHOLD


@pytest.mark.asyncio
async def test_get_current_spend_ok():
    rows = [[125.50, "USD"]]
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"properties": {"rows": rows}}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("budget_monitor.DefaultAzureCredential"), \
         patch("budget_monitor.get_bearer_token_provider", return_value=lambda: "tok"), \
         patch("budget_monitor.httpx.AsyncClient", return_value=mock_client):
        monitor = bm.BudgetMonitor("sub-123", "rg-test")
        status = await monitor.get_current_spend()

    assert status.actual_spend_usd == 125.50
    assert status.budget_limit_usd == bm.BUDGET_LIMIT_USD
    assert status.alert_level in ("ok", "warn", "critical")


@pytest.mark.asyncio
async def test_get_current_spend_handles_api_failure():
    """Budget monitor must not crash if Cost Management API is unreachable."""
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(side_effect=Exception("network error"))

    with patch("budget_monitor.DefaultAzureCredential"), \
         patch("budget_monitor.get_bearer_token_provider", return_value=lambda: "tok"), \
         patch("budget_monitor.httpx.AsyncClient", return_value=mock_client):
        monitor = bm.BudgetMonitor("sub-123", "rg-test")
        status = await monitor.get_current_spend()

    assert status.actual_spend_usd == 0.0
    assert status.alert_level == "ok"
