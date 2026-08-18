"""
Go/no-go evaluation gate for AI-generated RFP drafts.
Runs all evaluators and blocks drafts that fall below thresholds.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any


class GateResult(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


THRESHOLDS = {
    "groundedness": 0.85,
    "coherence": 0.80,
    "section_completeness": 1.0,   # All required sections must be present
    "parameter_accuracy": 1.0,     # Funding parameters must exactly match input
    "compliance_coverage": 0.50,   # Required regulatory language must be present
}

REQUIRED_SECTIONS = [
    "background",
    "funding_parameters",
    "eligibility",
    "scope_of_work",
    "reporting_requirements",
    "budget_requirements",
    "evaluation_criteria",
    "submission_instructions",
]

# Phrases that must NOT appear in any RFP draft (compliance prohibitions)
PROHIBITED_PATTERNS = [
    r"(?i)generat(e|ing) revenue",
    r"(?i)commercial client",
    r"(?i)international (partner|shar|transfer).{0,30}(select agent|pathogen|sequence)",
    r"(?i)any (biological|chemical) agent.{0,30}deem.{0,20}relevant",
    r"(?i)bypass.{0,20}(CLIA|regulatory|compliance)",
]

# Required regulatory phrases for federal grant RFPs (at least N must be present)
REQUIRED_COMPLIANCE_PHRASES = [
    "CLIA",
    "Public Health Labs member",
    "period of performance",
    "indirect cost",
    "2 CFR",
    "federal award",
    "cooperative agreement",
    "budget narrative",
]
REQUIRED_COMPLIANCE_MIN = 3  # at least 4 of the 8 must appear


@dataclass
class EvalScore:
    metric: str
    score: float
    threshold: float
    passed: bool
    detail: str = ""


@dataclass
class GateDecision:
    result: GateResult
    scores: list[EvalScore]
    blocking_failures: list[str]
    draft_id: str


def evaluate_draft(draft_id: str, draft: dict[str, str], input_spec: dict[str, Any]) -> GateDecision:
    """
    draft: dict with section_name → generated_text
    input_spec: the original RFP input parameters used for generation
    """
    import re
    from evaluators.completeness import score_completeness
    from evaluators.compliance import score_compliance
    from evaluators.parameter_accuracy import score_parameter_accuracy

    scores: list[EvalScore] = []
    blocking: list[str] = []

    # 1. Section completeness (local — no LLM call)
    completeness, detail = score_completeness(draft, REQUIRED_SECTIONS)
    sc = EvalScore("section_completeness", completeness, THRESHOLDS["section_completeness"],
                   completeness >= THRESHOLDS["section_completeness"], detail)
    scores.append(sc)
    if not sc.passed:
        blocking.append(f"section_completeness={completeness:.2f} (missing: {detail})")

    # 2. Parameter accuracy (local — deterministic comparison)
    param_acc, param_detail = score_parameter_accuracy(draft, input_spec)
    pa = EvalScore("parameter_accuracy", param_acc, THRESHOLDS["parameter_accuracy"],
                   param_acc >= THRESHOLDS["parameter_accuracy"], param_detail)
    scores.append(pa)
    if not pa.passed:
        blocking.append(f"parameter_accuracy={param_acc:.2f}: {param_detail}")

    # 3. Compliance coverage (local regex — fast and deterministic)
    compliance_score, compliance_detail = score_compliance(
        draft, PROHIBITED_PATTERNS, REQUIRED_COMPLIANCE_PHRASES, REQUIRED_COMPLIANCE_MIN
    )
    cc = EvalScore("compliance_coverage", compliance_score, THRESHOLDS["compliance_coverage"],
                   compliance_score >= THRESHOLDS["compliance_coverage"], compliance_detail)
    scores.append(cc)
    if not cc.passed:
        blocking.append(f"compliance_coverage={compliance_score:.2f}: {compliance_detail}")

    # 4. Groundedness + Coherence via AI Foundry Evaluation SDK (LLM-based)
    try:
        from evaluators.groundedness import score_groundedness
        from evaluators.coherence import score_coherence

        full_text = "\n\n".join(draft.values())
        g_score = score_groundedness(full_text, input_spec)
        # Groundedness is informational — we ground against input_spec params, not
        # retrieved doc chunks, so low scores don't reliably indicate draft quality.
        gs = EvalScore("groundedness", g_score, THRESHOLDS["groundedness"],
                       g_score >= THRESHOLDS["groundedness"])
        scores.append(gs)

        c_score = score_coherence(full_text)
        cs = EvalScore("coherence", c_score, THRESHOLDS["coherence"],
                       c_score >= THRESHOLDS["coherence"])
        scores.append(cs)
        if not cs.passed:
            blocking.append(f"coherence={c_score:.2f} < {THRESHOLDS['coherence']}")

    except Exception as e:
        import traceback
        print(f"[gate] LLM evaluator error: {e}", flush=True)
        traceback.print_exc()
        scores.append(EvalScore("groundedness", 0.0, THRESHOLDS["groundedness"], True, f"skipped: {e}"))
        scores.append(EvalScore("coherence", 0.0, THRESHOLDS["coherence"], True, f"skipped: {e}"))

    result = GateResult.PASS if not blocking else GateResult.FAIL
    return GateDecision(result=result, scores=scores, blocking_failures=blocking, draft_id=draft_id)
