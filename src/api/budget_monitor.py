"""
Budget monitoring for the Public Health RFP POC.
Queries Azure Cost Management to track spend vs the monthly budget
(MONTHLY_BUDGET_USD).
Also estimates per-request LLM costs from token usage.
Emits warnings via logging and App Insights when thresholds are approached.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Optional

import httpx
from azure.identity import DefaultAzureCredential, get_bearer_token_provider

log = logging.getLogger(__name__)

BUDGET_LIMIT_USD = float(os.getenv("MONTHLY_BUDGET_USD", "2500"))
WARN_THRESHOLD = float(os.getenv("BUDGET_WARN_THRESHOLD", "0.80"))     # 80%
CRITICAL_THRESHOLD = float(os.getenv("BUDGET_CRITICAL_THRESHOLD", "0.95"))  # 95%

# GPT-4o 2024-08-06 pricing (USD per 1K tokens)
GPT4O_PROMPT_RATE = 0.0025
GPT4O_COMPLETION_RATE = 0.010
# text-embedding-3-small is the model actually deployed (foundry.bicep) --
# was previously mislabeled/priced as text-embedding-3-large ($0.00013/1K).
EMBEDDING_RATE = 0.00002  # text-embedding-3-small per 1K tokens


@dataclass
class BudgetStatus:
    subscription_id: str
    resource_group: str
    period: str
    actual_spend_usd: float
    forecasted_spend_usd: float
    budget_limit_usd: float
    percent_actual: float
    percent_forecasted: float
    alert_level: str  # "ok", "warn", "critical"
    llm_session_cost_usd: float = 0.0
    llm_session_tokens: int = 0


@dataclass
class SessionCostTracker:
    """In-memory token/cost accumulator for the current API process lifetime."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    embedding_tokens: int = 0
    request_count: int = 0
    _cost_usd: float = field(default=0.0, init=False)

    def record_generation(self, prompt_tokens: int, completion_tokens: int) -> float:
        """Record token usage and return incremental cost."""
        cost = (
            (prompt_tokens / 1000) * GPT4O_PROMPT_RATE
            + (completion_tokens / 1000) * GPT4O_COMPLETION_RATE
        )
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.request_count += 1
        self._cost_usd += cost
        log.debug(
            "Generation cost: $%.4f | Session total: $%.4f (%d requests)",
            cost, self._cost_usd, self.request_count
        )
        return cost

    def record_embedding(self, tokens: int) -> float:
        cost = (tokens / 1000) * EMBEDDING_RATE
        self.embedding_tokens += tokens
        self._cost_usd += cost
        return cost

    @property
    def total_cost_usd(self) -> float:
        return self._cost_usd

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens + self.embedding_tokens


# Module-level singleton
session_tracker = SessionCostTracker()


class BudgetMonitor:
    def __init__(
        self,
        subscription_id: str,
        resource_group: str,
        credential: Optional[DefaultAzureCredential] = None,
    ):
        self._subscription_id = subscription_id
        self._resource_group = resource_group
        self._credential = credential or DefaultAzureCredential()
        self._token_provider = get_bearer_token_provider(
            self._credential, "https://management.azure.com/.default"
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token_provider()}"}

    async def get_current_spend(self) -> BudgetStatus:
        """Query Azure Cost Management for current month actual and forecasted spend."""
        now = date.today()
        start = now.replace(day=1).isoformat()
        end = now.isoformat()

        query_url = (
            f"https://management.azure.com/subscriptions/{self._subscription_id}"
            f"/resourceGroups/{self._resource_group}/providers/Microsoft.CostManagement"
            f"/query?api-version=2023-11-01"
        )
        body = {
            "type": "ActualCost",
            "timeframe": "Custom",
            "timePeriod": {"from": f"{start}T00:00:00Z", "to": f"{end}T23:59:59Z"},
            "dataset": {
                "granularity": "None",
                "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
            }
        }

        actual_spend = 0.0
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(query_url, headers=self._headers(), json=body)
                resp.raise_for_status()
                rows = resp.json().get("properties", {}).get("rows", [])
                actual_spend = rows[0][0] if rows else 0.0
        except Exception as e:
            log.warning("Failed to query actual spend: %s", e)

        forecasted_spend = actual_spend * (30 / max(now.day, 1))

        pct_actual = actual_spend / BUDGET_LIMIT_USD
        pct_forecast = forecasted_spend / BUDGET_LIMIT_USD

        if pct_forecast >= CRITICAL_THRESHOLD:
            alert_level = "critical"
            log.error("BUDGET CRITICAL: Forecasted $%.2f / $%.2f (%.0f%%)", forecasted_spend, BUDGET_LIMIT_USD, pct_forecast * 100)
        elif pct_actual >= WARN_THRESHOLD:
            alert_level = "warn"
            log.warning("BUDGET WARNING: Actual $%.2f / $%.2f (%.0f%%)", actual_spend, BUDGET_LIMIT_USD, pct_actual * 100)
        else:
            alert_level = "ok"

        return BudgetStatus(
            subscription_id=self._subscription_id,
            resource_group=self._resource_group,
            period=f"{start} to {end}",
            actual_spend_usd=round(actual_spend, 2),
            forecasted_spend_usd=round(forecasted_spend, 2),
            budget_limit_usd=BUDGET_LIMIT_USD,
            percent_actual=round(pct_actual, 4),
            percent_forecasted=round(pct_forecast, 4),
            alert_level=alert_level,
            llm_session_cost_usd=round(session_tracker.total_cost_usd, 4),
            llm_session_tokens=session_tracker.total_tokens,
        )

    @staticmethod
    def estimate_request_cost(prompt_tokens: int, completion_tokens: int) -> dict:
        """Estimate cost for a single generation request without hitting Azure."""
        cost = (prompt_tokens / 1000) * GPT4O_PROMPT_RATE + (completion_tokens / 1000) * GPT4O_COMPLETION_RATE
        remaining_budget = BUDGET_LIMIT_USD - session_tracker.total_cost_usd
        requests_remaining = int(remaining_budget / cost) if cost > 0 else 0
        return {
            "request_cost_usd": round(cost, 6),
            "session_cost_usd": round(session_tracker.total_cost_usd, 4),
            "session_request_count": session_tracker.request_count,
            "estimated_requests_remaining_in_budget": requests_remaining,
        }
