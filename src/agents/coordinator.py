"""
Coordinator Agent — routes intent to the appropriate specialist agent and assembles the response.

Intent routing:
  generate_rfp       → RfpGenerationAgent
  review_proposal    → ProposalReviewAgent
  classify           → ClassificationAgent
  regulatory_watch   → RegulatoryWatchAgent
  audit_budget       → BudgetAuditAgent
"""
from __future__ import annotations
from .classification_agent import ClassificationAgent
from .generation_agent import RfpGenerationAgent
from .review_agent import ProposalReviewAgent
from .regulatory_agent import RegulatoryWatchAgent
from .budget_agent import BudgetAuditAgent

import re


_INTENT_PATTERNS = [
    (re.compile(r"\b(generate|draft|write|create).{0,30}rfp\b", re.I), "generate_rfp"),
    (re.compile(r"\b(review|score|evaluate).{0,30}proposal\b", re.I), "review_proposal"),
    (re.compile(r"\b(classify|categorize|what program)\b", re.I), "classify"),
    (re.compile(r"\b(regulatory|federal register|cfr|guidance change)\b", re.I), "regulatory_watch"),
    (re.compile(r"\b(budget|allowable|indirect cost|cost principle)\b", re.I), "audit_budget"),
]


class CoordinatorAgent:
    def __init__(self) -> None:
        self._agents: dict = {}

    def _get_agent(self, intent: str):
        if intent not in self._agents:
            mapping = {
                "generate_rfp": RfpGenerationAgent,
                "review_proposal": ProposalReviewAgent,
                "classify": ClassificationAgent,
                "regulatory_watch": RegulatoryWatchAgent,
                "audit_budget": BudgetAuditAgent,
            }
            self._agents[intent] = mapping[intent]()
        return self._agents[intent]

    def route(self, message: str) -> str:
        intent = self._detect_intent(message)
        if intent is None:
            return (
                "I can help with: drafting RFPs, reviewing proposals, classifying program areas, "
                "monitoring regulatory changes, or auditing budgets. Which would you like?"
            )
        agent = self._get_agent(intent)
        return agent.run(message)

    def _detect_intent(self, message: str) -> str | None:
        for pattern, intent in _INTENT_PATTERNS:
            if pattern.search(message):
                return intent
        return None

    def cleanup(self) -> None:
        for agent in self._agents.values():
            agent.delete()
        self._agents.clear()
