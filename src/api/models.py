from __future__ import annotations
from pydantic import BaseModel, Field, model_validator
from typing import Optional
from enum import Enum


class GateDecision(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


class RfpRequest(BaseModel):
    program_area: str
    federal_sponsor: str
    total_funding: float = Field(gt=0, le=200_000_000)
    period_of_performance_months: int = Field(ge=1, le=120)
    fiscal_year: Optional[str] = None
    award_range_min: Optional[float] = None
    award_range_max: Optional[float] = None
    estimated_awards_min: Optional[int] = None
    estimated_awards_max: Optional[int] = None
    cost_sharing_required: bool = False
    key_requirements: list[str] = []
    similar_rfp_ids: list[str] = []
    sharepoint_output_library: Optional[str] = None
    write_to_sharepoint: bool = False
    write_to_fabric: bool = True


class GroundingChunk(BaseModel):
    chunk_id: str
    rfp_id: str
    section_type: str
    score: float
    content_preview: str


class TokenUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class RfpDraft(BaseModel):
    draft_id: str
    rfp_id: str
    program_area: str
    federal_sponsor: str
    generated_at: str
    sections: dict[str, str]
    grounding_chunks: list[GroundingChunk] = []
    token_usage: TokenUsage = TokenUsage()
    sharepoint_url: Optional[str] = None
    fabric_lakehouse_path: Optional[str] = None


class EvaluatorScores(BaseModel):
    completeness: float
    parameter_accuracy: float
    compliance: float
    groundedness: float
    coherence: float


class EvaluationResult(BaseModel):
    draft_id: str
    rfp_id: str
    gate_decision: GateDecision
    scores: EvaluatorScores
    failure_reasons: list[str] = []
    passed_threshold: dict[str, bool] = {}


class GenerateAndEvaluateResponse(BaseModel):
    draft: RfpDraft
    evaluation: EvaluationResult
    gate_decision: GateDecision


class HealthResponse(BaseModel):
    status: str
    orchestrator_reachable: bool
    search_reachable: bool
    sharepoint_configured: bool
    fabric_configured: bool


# ── Classification ───────────────────────────────────────────────────────────────

class ClassifyRequest(BaseModel):
    text: str


class ClassifyResult(BaseModel):
    program_area: str
    confidence: float
    rationale: str


# ── Proposal Review ──────────────────────────────────────────────────────────────

class ReviewRequest(BaseModel):
    proposal_text: str
    rfp_id: Optional[str] = None
    program_area: Optional[str] = None
    evaluation_criteria: list[str] = [
        "Technical Approach", "Organizational Capacity",
        "Personnel Qualifications", "Budget Justification", "Evaluation Plan",
    ]


class ReviewScore(BaseModel):
    score: int
    evidence: str
    flags: list[str] = []


class ReviewResult(BaseModel):
    scores: dict[str, ReviewScore]
    total_score: int
    recommendation: str
    flags: list[str] = []


# ── Budget Audit ─────────────────────────────────────────────────────────────────

class BudgetAuditRequest(BaseModel):
    budget_text: str
    total_funding: float
    program_area: Optional[str] = None


class BudgetLineItem(BaseModel):
    item: str
    allowable: bool
    issue: Optional[str] = None


class BudgetAuditResult(BaseModel):
    total_verified: bool
    line_items: list[BudgetLineItem] = []
    flags: list[str] = []
    recommendation: str


# ── Regulatory Watch ─────────────────────────────────────────────────────────────

class RegulatoryAlert(BaseModel):
    regulation: str
    effective_date: str
    change_summary: str
    affected_programs: list[str] = []
    action_required: str


class RegulatoryWatchResult(BaseModel):
    alerts: list[RegulatoryAlert] = []
    checked_at: str
    days_back: int
