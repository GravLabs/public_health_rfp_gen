"""Prompt Flow node: run evaluation gate and return scores + pass/fail."""
from promptflow import tool
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "evaluation"))

from gate import evaluate_draft, GateResult
from evaluators.completeness import score_completeness
from evaluators.compliance import score_compliance
from evaluators.parameter_accuracy import score_parameter_accuracy

REQUIRED_SECTIONS = [
    "background", "funding_parameters", "eligibility", "scope_of_work",
    "reporting_requirements", "budget_requirements", "evaluation_criteria", "submission_instructions",
]
PROHIBITED_PATTERNS = [
    r"(?i)generat(e|ing) revenue",
    r"(?i)commercial client",
    r"(?i)bypass.{0,20}(CLIA|regulatory|compliance)",
]
REQUIRED_COMPLIANCE_PHRASES = [
    "CLIA", "Public Health Labs member", "indirect cost", "2 CFR",
]


@tool
def evaluate_node(draft: dict, params: dict, chunks: list[dict]) -> dict:
    sections = draft.get("draft", draft)
    draft_id = "promptflow-draft"

    completeness_score, completeness_reason = score_completeness(sections, REQUIRED_SECTIONS)
    compliance_score, compliance_reason = score_compliance(
        {"full": "\n\n".join(sections.values())},
        PROHIBITED_PATTERNS,
        REQUIRED_COMPLIANCE_PHRASES,
        min_phrases=2,
    )
    param_score, param_reason = score_parameter_accuracy(sections, params)

    gate_scores = {
        "completeness": {"score": completeness_score, "reason": completeness_reason},
        "compliance": {"score": compliance_score, "reason": compliance_reason},
        "parameter_accuracy": {"score": param_score, "reason": param_reason},
    }

    passed = completeness_score >= 0.7 and compliance_score >= 0.8 and param_score >= 1.0

    return {"gate_scores": gate_scores, "passed": passed}
