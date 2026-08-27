"""Unit tests for the evaluation gate and all 5 evaluators."""
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "evaluation"))

REQUIRED_SECTIONS = [
    "background", "funding_parameters", "eligibility", "scope_of_work",
    "reporting_requirements", "budget_requirements", "evaluation_criteria", "submission_instructions",
]

PROHIBITED_PATTERNS = [
    r"(?i)generat(e|ing) revenue",
    r"(?i)commercial client",
    r"(?i)international (partner|shar|transfer).{0,30}(select agent|pathogen|sequence)",
    r"(?i)any (biological|chemical) agent.{0,30}deem.{0,20}relevant",
    r"(?i)bypass.{0,20}(CLIA|regulatory|compliance)",
]

REQUIRED_COMPLIANCE_PHRASES = [
    "CLIA", "Public Health Labs member", "period of performance",
    "indirect cost", "2 CFR", "federal award", "principal investigator", "budget justification",
]
REQUIRED_COMPLIANCE_MIN = 4


# ── Completeness evaluator ──────────────────────────────────────────────────

def test_completeness_all_sections_pass():
    from evaluators.completeness import score_completeness
    sections = {
        "background": "Public health laboratory surveillance " * 40,
        "funding_parameters": "Total funding $3.5M, awards 15-20, period 24 months " * 10,
        "eligibility": "Public Health Labs member, CLIA certified, LRN participant " * 10,
        "scope_of_work": "Section A: Implement WGS. B: Surge capacity. C: Reporting. D: QA. " * 30,
        "reporting_requirements": "Quarterly reports to CDC. Final report 90 days. " * 10,
        "budget_requirements": "Reagents allowable, 2 CFR 200 compliance required. " * 10,
        "evaluation_criteria": "Technical approach 30 pts, capacity 25 pts, budget 20 pts. " * 10,
        "submission_instructions": "Deadline May 15, 2024. Submit via Public Health Labs Grants Portal. " * 10,
    }
    score, _ = score_completeness(sections, REQUIRED_SECTIONS)
    assert score == 1.0


def test_completeness_missing_section_penalizes():
    from evaluators.completeness import score_completeness
    sections = {"background": "Background text " * 30}
    score, _ = score_completeness(sections, REQUIRED_SECTIONS)
    assert score < 0.3


def test_completeness_short_sections_penalize():
    from evaluators.completeness import score_completeness
    sections = {k: "text" for k in REQUIRED_SECTIONS}
    score, _ = score_completeness(sections, REQUIRED_SECTIONS)
    assert score < 0.5


def test_completeness_detail_is_human_readable():
    from evaluators.completeness import score_completeness
    sections = {k: "Some real content here." for k in REQUIRED_SECTIONS}
    sections["background"] = "one"
    del sections["eligibility"]
    _, detail = score_completeness(sections, REQUIRED_SECTIONS)
    assert "Background is too short (1 word, needs at least 50)" in detail
    assert "Missing section: Eligibility" in detail
    # No raw Python-repr fragments (old format: thin_sections=['background(1<50w)'])
    assert "thin_sections" not in detail
    assert "[" not in detail


# ── Compliance evaluator ────────────────────────────────────────────────────

def test_compliance_clean_text_passes():
    from evaluators.compliance import score_compliance
    draft = {"full": (
        "All activities must comply with 2 CFR Part 200. CLIA high-complexity certification required. "
        "Federal funds may not supplant existing state funds. No profit may be derived. "
        "Human subjects research must comply with 45 CFR Part 46. indirect cost at negotiated rate. "
        "All expenses must be allowable, allocable, and reasonable. federal award criteria apply. "
        "Public Health Labs member laboratories are eligible. period of performance is 24 months. "
        "principal investigator must be named. budget justification is required."
    )}
    score, _ = score_compliance(draft, PROHIBITED_PATTERNS, REQUIRED_COMPLIANCE_PHRASES, REQUIRED_COMPLIANCE_MIN)
    assert score >= 0.8


def test_compliance_detects_commercial_use():
    from evaluators.compliance import score_compliance
    draft = {"full": "This award authorizes commercial use of the generated content for revenue generation. " * 5}
    score, _ = score_compliance(draft, PROHIBITED_PATTERNS, REQUIRED_COMPLIANCE_PHRASES, REQUIRED_COMPLIANCE_MIN)
    assert score == 0.0


def test_compliance_missing_required_phrases_penalizes():
    from evaluators.compliance import score_compliance
    draft = {"full": "This is a generic grant document with no compliance language. Awards available."}
    score, _ = score_compliance(draft, PROHIBITED_PATTERNS, REQUIRED_COMPLIANCE_PHRASES, REQUIRED_COMPLIANCE_MIN)
    assert score < 0.5


# ── Parameter accuracy evaluator ────────────────────────────────────────────

def test_parameter_accuracy_exact_match():
    from evaluators.parameter_accuracy import score_parameter_accuracy
    draft = {"funding_parameters": "Total funding: $3,500,000. Period of performance: 24 months. Cost sharing not required."}
    params = {"total_funding": 3_500_000, "period_of_performance_months": 24, "cost_sharing": "No"}
    score, _ = score_parameter_accuracy(draft, params)
    assert score >= 0.9


def test_parameter_accuracy_wrong_total_fails():
    from evaluators.parameter_accuracy import score_parameter_accuracy
    draft = {"funding_parameters": "Total funding available: $5,000,000. Period of performance: 24 months."}
    params = {"total_funding": 3_500_000, "period_of_performance_months": 24, "cost_sharing": "No"}
    score, _ = score_parameter_accuracy(draft, params)
    # Gate threshold is 1.0 — any parameter mismatch causes failure
    assert score < 1.0


def test_parameter_accuracy_within_tolerance():
    from evaluators.parameter_accuracy import score_parameter_accuracy
    draft = {"funding_parameters": "Total funding: $3,510,000. Period: 24 months. No cost sharing."}
    params = {"total_funding": 3_500_000, "period_of_performance_months": 24, "cost_sharing": "No"}
    score, _ = score_parameter_accuracy(draft, params)
    assert score >= 0.85


def test_parameter_accuracy_wrong_cost_sharing():
    from evaluators.parameter_accuracy import score_parameter_accuracy
    draft = {"funding_parameters": "Total funding $3.5M. 24 months. Cost sharing required at 10% of award."}
    params = {"total_funding": 3_500_000, "period_of_performance_months": 24, "cost_sharing": "No"}
    score, _ = score_parameter_accuracy(draft, params)
    assert score < 0.8


# ── Gate integration ────────────────────────────────────────────────────────

def _make_full_sections(**overrides):
    base = {
        "background": "Public Health Labs state laboratory cooperative agreement. 2 CFR Part 200 compliance required. federal award recipients must comply. " * 20,
        "funding_parameters": "Total funding $3,500,000. Awards 15-20 laboratories. Period of performance 24 months. No cost sharing. " * 15,
        "eligibility": "Public Health Labs member, CLIA high-complexity. Federal funds not supplanting. principal investigator must be named. " * 15,
        "scope_of_work": "A. Implement WGS. B. Surge capacity 200 specimens/day. C. Report quarterly. D. QA protocol. " * 30,
        "reporting_requirements": "Quarterly reports. Annual report. Final report 90 days post-period. Allowable costs. " * 15,
        "budget_requirements": "Reagents allowable. Personnel time allowable. 2 CFR 200 indirect cost at negotiated rate. budget justification required. " * 15,
        "evaluation_criteria": "Technical approach 30 pts. Capacity 25 pts. Partnerships 20 pts. Budget 15 pts. QA 10 pts. " * 10,
        "submission_instructions": "Deadline May 15, 2024, 5 PM ET. Public Health Labs Grants Portal. Narrative, Budget, CLIA certificate required. " * 10,
    }
    base.update(overrides)
    return base


def test_gate_passes_on_good_draft():
    import sys
    from gate import evaluate_draft, GateResult
    from unittest.mock import MagicMock, patch
    sections = _make_full_sections()
    params = {"total_funding": 3_500_000, "period_of_performance_months": 24, "cost_sharing": "No"}
    # azure.ai.evaluation isn't installed locally; inject fake modules so gate.py's
    # inline `from evaluators.groundedness import score_groundedness` gets our stubs
    fake_g = MagicMock()
    fake_g.score_groundedness = MagicMock(return_value=0.9)
    fake_c = MagicMock()
    fake_c.score_coherence = MagicMock(return_value=0.85)
    with patch.dict(sys.modules, {"evaluators.groundedness": fake_g, "evaluators.coherence": fake_c}):
        decision = evaluate_draft("test-draft-001", sections, params)
    assert decision.result == GateResult.PASS


def test_gate_fails_on_incomplete_draft():
    from gate import evaluate_draft, GateResult
    sections = {"background": "Short text only."}
    params = {"total_funding": 3_500_000, "period_of_performance_months": 24, "cost_sharing": "No"}
    decision = evaluate_draft("test-draft-002", sections, params)
    assert decision.result == GateResult.FAIL
    completeness_score = next(s for s in decision.scores if s.metric == "section_completeness")
    assert not completeness_score.passed
    assert any("missing" in r.lower() for r in decision.blocking_failures)
