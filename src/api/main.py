"""
Public Health RFP Generation Review API
FastAPI service that:
  1. Accepts RFP generation requests
  2. Calls the .NET Semantic Kernel orchestrator for draft generation
  3. Runs the Python evaluation gate
  4. Optionally writes drafts back to SharePoint and/or Fabric Lakehouse
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from azure.identity import DefaultAzureCredential
from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings
from botbuilder.schema import Activity
from fastapi import FastAPI, HTTPException, BackgroundTasks, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from bot import RfpBotHandler
from models import (
    RfpRequest, RfpDraft, EvaluationResult, GenerateAndEvaluateResponse,
    EvaluatorScores, GateDecision, HealthResponse, TokenUsage,
    ClassifyRequest, ClassifyResult,
    ReviewRequest, ReviewResult, ReviewScore,
    BudgetAuditRequest, BudgetAuditResult, BudgetLineItem,
    RegulatoryWatchResult, RegulatoryAlert,
)
from sharepoint_client import SharePointClient
from fabric_client import FabricClient
from observability import setup_observability, tracer, record_generation_span, record_evaluation_span
from budget_monitor import BudgetMonitor, session_tracker
from foundry_client import FoundryTracer, FoundryProjectClient

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:5001")
SHAREPOINT_SITE_ID = os.getenv("SHAREPOINT_SITE_ID", "")
SHAREPOINT_LIBRARY = os.getenv("SHAREPOINT_LIBRARY", "RFP Corpus")
SHAREPOINT_DRAFT_LIBRARY = os.getenv("SHAREPOINT_DRAFT_LIBRARY", "Generated Drafts")
FABRIC_WORKSPACE_ID = os.getenv("FABRIC_WORKSPACE_ID", "")
FABRIC_LAKEHOUSE_ID = os.getenv("FABRIC_LAKEHOUSE_ID", "")
AZURE_SUBSCRIPTION_ID = os.getenv("AZURE_SUBSCRIPTION_ID", "")
AZURE_RESOURCE_GROUP = os.getenv("AZURE_RESOURCE_GROUP", "")

credential = DefaultAzureCredential()

# ── Teams Bot adapter ──────────────────────────────────────────────────────────
_bot_adapter = BotFrameworkAdapter(BotFrameworkAdapterSettings(
    app_id=os.getenv("MICROSOFT_APP_ID", ""),
    app_password=os.getenv("MICROSOFT_APP_PASSWORD", ""),
    channel_auth_tenant=os.getenv("MICROSOFT_APP_TENANT_ID", ""),
))

_sp_client: Optional[SharePointClient] = None
_fabric_client: Optional[FabricClient] = None
_budget_monitor: Optional[BudgetMonitor] = None
_foundry_project_client: Optional[FoundryProjectClient] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _sp_client, _fabric_client, _budget_monitor, _foundry_project_client

    # AI Foundry tracing — must be first
    FoundryTracer.setup()
    setup_observability(app)

    if SHAREPOINT_SITE_ID:
        _sp_client = SharePointClient(SHAREPOINT_SITE_ID, credential)
        log.info("SharePoint client initialized for site: %s", SHAREPOINT_SITE_ID)
    if FABRIC_WORKSPACE_ID and FABRIC_LAKEHOUSE_ID:
        _fabric_client = FabricClient(FABRIC_WORKSPACE_ID, FABRIC_LAKEHOUSE_ID, credential)
        log.info("Fabric client initialized for workspace: %s", FABRIC_WORKSPACE_ID)
    if AZURE_SUBSCRIPTION_ID and AZURE_RESOURCE_GROUP:
        _budget_monitor = BudgetMonitor(AZURE_SUBSCRIPTION_ID, AZURE_RESOURCE_GROUP, credential)
        log.info("Budget monitor initialized")
    _foundry_project_client = FoundryProjectClient(credential)
    yield


app = FastAPI(
    title="Public Health RFP Generation API",
    description="AI-powered RFP generation with SharePoint and Fabric integration",
    version="0.1.0",
    lifespan=lifespan,
)

# In-memory draft cache keyed by draft_id — populated after gate PASS, used by /export
_draft_cache: dict[str, dict] = {}


async def _call_orchestrator(request: RfpRequest) -> dict:
    """Call the .NET Semantic Kernel orchestrator service."""
    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(
            f"{ORCHESTRATOR_URL}/generate",
            json=request.model_dump(exclude={"write_to_sharepoint", "write_to_fabric"})
        )
        if resp.status_code == 422:
            raise HTTPException(status_code=422, detail=resp.json())
        resp.raise_for_status()
        return resp.json()


async def _run_evaluation(draft: RfpDraft, request: RfpRequest) -> EvaluationResult:
    """Import and run the Python evaluation gate against the generated draft."""
    import sys
    sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent / "evaluation"))
    from gate import evaluate_draft

    params = {
        "total_funding": request.total_funding,
        "period_of_performance_months": request.period_of_performance_months,
        "cost_sharing_required": request.cost_sharing_required,
        "award_range_min": request.award_range_min,
        "award_range_max": request.award_range_max,
    }

    decision = evaluate_draft(
        draft_id=draft.draft_id,
        draft=draft.sections,
        input_spec=params,
    )

    scores_dict = {s.metric: s.score for s in decision.scores}
    passed_threshold = {s.metric: s.passed for s in decision.scores}

    from gate import GateResult as _GateResult
    return EvaluationResult(
        draft_id=draft.draft_id,
        rfp_id=draft.rfp_id,
        gate_decision=GateDecision.PASS if decision.result == _GateResult.PASS else GateDecision.FAIL,
        scores=EvaluatorScores(
            completeness=scores_dict.get("section_completeness", 0.0),
            parameter_accuracy=scores_dict.get("parameter_accuracy", 0.0),
            compliance=scores_dict.get("compliance_coverage", 0.0),
            groundedness=scores_dict.get("groundedness", 0.0),
            coherence=scores_dict.get("coherence", 0.0),
        ),
        failure_reasons=decision.blocking_failures,
        passed_threshold=passed_threshold,
    )


async def _persist_draft(draft: RfpDraft, request: RfpRequest, evaluation: EvaluationResult) -> RfpDraft:
    """Persist draft to SharePoint and/or Fabric Lakehouse."""
    sp_url = None
    fabric_path = None

    if request.write_to_sharepoint and _sp_client:
        sp_url = await _sp_client.upload_draft_docx(
            SHAREPOINT_DRAFT_LIBRARY, draft.rfp_id, draft.draft_id, draft.sections
        )
        await _sp_client.create_review_item("RFP Review Tracking", {
            "Title": draft.rfp_id,
            "DraftId": draft.draft_id,
            "GateDecision": evaluation.gate_decision.value,
            "CompletenessScore": evaluation.scores.completeness,
            "GroundednessScore": evaluation.scores.groundedness,
            "SharePointUrl": sp_url,
            "Status": "Pending Review"
        })

    if request.write_to_fabric and _fabric_client:
        md_content = SharePointClient.draft_to_markdown(draft.rfp_id, draft.sections)
        fabric_path = await _fabric_client.write_draft_to_lakehouse(draft.draft_id, draft.rfp_id, md_content)
        await _fabric_client.write_eval_record({
            "draft_id": draft.draft_id,
            "rfp_id": draft.rfp_id,
            "program_area": draft.program_area,
            "gate_decision": evaluation.gate_decision.value,
            "scores": evaluation.scores.model_dump(),
            "failure_reasons": evaluation.failure_reasons,
            "token_usage": draft.token_usage.model_dump(),
            "generated_at": draft.generated_at,
        })

    return draft.model_copy(update={"sharepoint_url": sp_url, "fabric_lakehouse_path": fabric_path})


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.post("/generate", response_model=RfpDraft, summary="Generate an RFP draft")
async def generate(request: RfpRequest) -> RfpDraft:
    """Generate an RFP draft without evaluation. Fast path for iteration."""
    raw = await _call_orchestrator(request)
    return RfpDraft(**raw)


@app.post("/generate-and-evaluate", response_model=GenerateAndEvaluateResponse,
          summary="Generate and evaluate an RFP draft in one call")
async def generate_and_evaluate(request: RfpRequest) -> GenerateAndEvaluateResponse:
    """Full pipeline: generate → evaluate gate → optionally persist to SharePoint/Fabric."""
    raw = await _call_orchestrator(request)
    draft = RfpDraft(**raw)
    evaluation = await _run_evaluation(draft, request)
    draft = await _persist_draft(draft, request, evaluation)

    if evaluation.gate_decision == GateDecision.PASS:
        _draft_cache[draft.draft_id] = {"rfp_id": draft.rfp_id, "sections": draft.sections}

    return GenerateAndEvaluateResponse(
        draft=draft,
        evaluation=evaluation,
        gate_decision=evaluation.gate_decision,
    )


@app.post("/generate/stream", summary="Generate and evaluate with streaming progress (NDJSON)")
async def generate_stream(request: RfpRequest):
    """
    Streams NDJSON events as each RFP section completes, then emits a gate_result.

    Event types:
      {"type": "started",      "draft_id": "...", "rfp_id": "..."}
      {"type": "section",      "section_key": "...", "index": N, "total": 8}
      {"type": "gate_result",  "passed": bool, "scores": {...},
                               "sharepoint_url": "...", "failure_reasons": [...]}
      {"type": "error",        "message": "..."}
    """
    async def _events():
        sections: dict[str, str] = {}
        draft_id = uuid.uuid4().hex[:12]
        area_slug = request.program_area[:8].upper().replace(" ", "-")
        rfp_id = f"Public Health Labs-RFP-{area_slug}-{draft_id[:6]}"

        try:
            yield json.dumps({"type": "started", "draft_id": draft_id, "rfp_id": rfp_id}) + "\n"

            async with httpx.AsyncClient(timeout=300) as client:
                async with client.stream(
                    "POST", f"{ORCHESTRATOR_URL}/generate/stream",
                    json=request.model_dump(exclude={"write_to_sharepoint", "write_to_fabric"})
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        evt = json.loads(line)
                        if evt["type"] == "section":
                            sections[evt["sectionKey"]] = evt.get("sectionText", "")
                            yield json.dumps({
                                "type": "section",
                                "section_key": evt["sectionKey"],
                                "index": evt["index"],
                                "total": evt["total"],
                            }) + "\n"

            # Evaluation + persistence after all sections collected
            draft = RfpDraft(
                draft_id=draft_id,
                rfp_id=rfp_id,
                program_area=request.program_area,
                federal_sponsor=request.federal_sponsor,
                generated_at=datetime.utcnow().isoformat() + "Z",
                sections=sections,
                grounding_chunks=[],
                token_usage=TokenUsage(prompt_tokens=0, completion_tokens=0),
            )
            evaluation = await _run_evaluation(draft, request)
            draft = await _persist_draft(draft, request, evaluation)

            if evaluation.gate_decision == GateDecision.PASS:
                _draft_cache[draft.draft_id] = {"rfp_id": draft.rfp_id, "sections": sections}

            yield json.dumps({
                "type": "gate_result",
                "passed": evaluation.gate_decision == GateDecision.PASS,
                "scores": evaluation.scores.model_dump(),
                "failure_reasons": evaluation.failure_reasons,
                "sharepoint_url": draft.sharepoint_url or "",
                "draft_id": draft.draft_id,
                "rfp_id": draft.rfp_id,
                "sections": sections,
            }) + "\n"

        except Exception as exc:
            log.exception("Streaming generation error: %s", exc)
            yield json.dumps({"type": "error", "message": str(exc)}) + "\n"

    return StreamingResponse(_events(), media_type="application/x-ndjson")


@app.post("/evaluate", response_model=EvaluationResult, summary="Evaluate an existing draft")
async def evaluate(draft: RfpDraft, request: RfpRequest) -> EvaluationResult:
    """Run the evaluation gate on an already-generated draft."""
    return await _run_evaluation(draft, request)


@app.post("/export/{draft_id}", summary="Upload approved draft to SharePoint and return link")
async def export_draft(draft_id: str):
    """Upload a gate-passed draft to SharePoint. Called after user approval."""
    cached = _draft_cache.get(draft_id)
    if not cached:
        raise HTTPException(status_code=404, detail="Draft not found or gate did not pass")
    if not _sp_client:
        raise HTTPException(status_code=503, detail="SharePoint not configured")

    try:
        sp_url = await _sp_client.upload_draft_docx(
            SHAREPOINT_DRAFT_LIBRARY, cached["rfp_id"], draft_id, cached["sections"]
        )
    except Exception as exc:
        log.exception("SharePoint upload failed for draft %s", draft_id)
        raise HTTPException(status_code=500, detail=f"SharePoint upload failed: {exc}") from exc

    return {"sharepoint_url": sp_url, "draft_id": draft_id, "rfp_id": cached["rfp_id"]}


@app.get("/sharepoint/corpus", summary="List SharePoint corpus files")
async def list_sharepoint_corpus():
    """List files available in the SharePoint RFP corpus library."""
    if not _sp_client:
        raise HTTPException(status_code=503, detail="SharePoint not configured (SHAREPOINT_SITE_ID not set)")
    files = await _sp_client.list_library_files(SHAREPOINT_LIBRARY)
    return {"library": SHAREPOINT_LIBRARY, "files": files, "count": len(files)}


@app.post("/sharepoint/ingest", summary="Trigger SharePoint corpus ingestion")
async def ingest_sharepoint(background_tasks: BackgroundTasks):
    """Download all RFP documents from SharePoint and re-index into AI Search."""
    if not _sp_client:
        raise HTTPException(status_code=503, detail="SharePoint not configured")

    async def _ingest():
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parent.parent / "ingestion"))
        import tempfile
        from pathlib import Path as P
        from pipeline import run_pipeline

        with tempfile.TemporaryDirectory() as tmp:
            downloaded = await _sp_client.download_library_to_local(SHAREPOINT_LIBRARY, P(tmp))
            log.info("Downloaded %d files from SharePoint; running ingestion pipeline", len(downloaded))
            for path in downloaded:
                await run_pipeline(str(path))

    background_tasks.add_task(_ingest)
    return {"status": "ingestion_started", "source": "sharepoint"}


@app.get("/budget", summary="Current Azure budget status and LLM cost estimate")
async def budget_status():
    """Returns current Azure spend vs $500 budget and per-session LLM cost accumulation."""
    if not _budget_monitor:
        return {
            "status": "budget_monitor_not_configured",
            "session": BudgetMonitor.estimate_request_cost(0, 0),
            "note": "Set AZURE_SUBSCRIPTION_ID and AZURE_RESOURCE_GROUP to enable Azure Cost Management"
        }
    status = await _budget_monitor.get_current_spend()
    return {
        "period": status.period,
        "actual_spend_usd": status.actual_spend_usd,
        "forecasted_spend_usd": status.forecasted_spend_usd,
        "budget_limit_usd": status.budget_limit_usd,
        "percent_actual": status.percent_actual,
        "percent_forecasted": status.percent_forecasted,
        "alert_level": status.alert_level,
        "session_llm_cost_usd": status.llm_session_cost_usd,
        "session_total_tokens": status.llm_session_tokens,
        "session_request_count": session_tracker.request_count,
    }


@app.get("/foundry/info", summary="AI Foundry project information")
async def foundry_info():
    """Returns AI Foundry project configuration and available deployments."""
    if not _foundry_project_client:
        return {"status": "not_configured"}
    info = await _foundry_project_client.get_project_info()
    deployments = await _foundry_project_client.list_deployments()
    return {**info, "deployments": [d.get("name") for d in deployments]}


# ── Azure OpenAI helper (used by agent routes) ────────────────────────────────

async def _azure_openai_chat(messages: list[dict], *, max_tokens: int = 2000, json_mode: bool = False) -> str:
    from azure.identity.aio import DefaultAzureCredential as AsyncCred
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
    deployment = os.getenv("AZURE_OPENAI_GPT_DEPLOYMENT", "gpt-4o")
    async with AsyncCred() as cred:
        token = await cred.get_token("https://cognitiveservices.azure.com/.default")
    url = f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version=2024-06-01"
    body: dict = {"messages": messages, "max_tokens": max_tokens, "temperature": 0.1}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(url, headers={
            "Authorization": f"Bearer {token.token}",
            "Content-Type": "application/json",
        }, json=body)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


# ── Capability routes ─────────────────────────────────────────────────────────

_TAXONOMY = (
    "Influenza Surveillance | Whole Genome Sequencing | Antimicrobial Resistance | "
    "Food Safety | Emergency Preparedness | HIV/STI Testing | Tuberculosis | "
    "COVID-19 / Respiratory Pathogens | Bioterrorism / LRN | General Surveillance"
)


@app.post("/classify", response_model=ClassifyResult, summary="Classify program area from free text")
async def classify_program_area(body: ClassifyRequest) -> ClassifyResult:
    content = await _azure_openai_chat([
        {"role": "system", "content": (
            f"You are a public health program area classifier. Map the description to exactly one taxonomy entry: {_TAXONOMY}. "
            "Return JSON with keys: program_area (str), confidence (float 0-1), rationale (str)."
        )},
        {"role": "user", "content": body.text},
    ], max_tokens=300, json_mode=True)
    return ClassifyResult(**json.loads(content))


@app.post("/review", response_model=ReviewResult, summary="Score a proposal against evaluation criteria")
async def review_proposal(body: ReviewRequest) -> ReviewResult:
    criteria_list = "\n".join(f"- {c} (max 20 points)" for c in body.evaluation_criteria)
    content = await _azure_openai_chat([
        {"role": "system", "content": (
            "You are a public health grants reviewer. Score each criterion 0-20 based on the proposal text. "
            "Return JSON with keys: scores (object mapping criterion name to {score: int, evidence: str, flags: [str]}), "
            "total_score (int), recommendation ('fund'|'revise'|'reject'), flags ([str])."
        )},
        {"role": "user", "content": (
            f"Evaluation criteria:\n{criteria_list}\n\n"
            f"Program area: {body.program_area or 'Public Health Laboratory'}\n\n"
            f"Proposal text:\n{body.proposal_text[:8000]}"
        )},
    ], max_tokens=2000, json_mode=True)
    data = json.loads(content)
    scores = {k: ReviewScore(**v) for k, v in data.get("scores", {}).items()}
    return ReviewResult(
        scores=scores,
        total_score=data.get("total_score", 0),
        recommendation=data.get("recommendation", "revise"),
        flags=data.get("flags", []),
    )


@app.post("/audit_budget", response_model=BudgetAuditResult, summary="Audit a budget narrative for allowability")
async def audit_budget(body: BudgetAuditRequest) -> BudgetAuditResult:
    content = await _azure_openai_chat([
        {"role": "system", "content": (
            "You are a federal grants budget compliance auditor (2 CFR Part 200). "
            "Review each budget line for allowability and arithmetic accuracy. "
            "Return JSON with keys: total_verified (bool), "
            "line_items (list of {item: str, allowable: bool, issue: str or null}), "
            "flags ([str]), recommendation ('approve'|'revise'|'reject')."
        )},
        {"role": "user", "content": (
            f"Total award amount: ${body.total_funding:,.0f}\n"
            f"Program area: {body.program_area or 'Public Health Laboratory'}\n\n"
            f"Budget narrative:\n{body.budget_text[:8000]}"
        )},
    ], max_tokens=2000, json_mode=True)
    data = json.loads(content)
    line_items = [BudgetLineItem(**li) for li in data.get("line_items", [])]

    # GPT is unreliable at arithmetic — strip arithmetic mismatch flags and compute ourselves.
    # A budget is verified if the stated total appears verbatim in the budget text.
    import re as _re
    flags = [f for f in data.get("flags", [])
             if not _re.search(r"arithmeti|does not match|sum|total.*exceed|exceed.*total", f, _re.I)]
    total_str = f"{int(body.total_funding):,}"
    total_verified = total_str in body.budget_text or str(int(body.total_funding)) in body.budget_text

    has_issues = any(not li.allowable for li in line_items) or bool(flags)
    recommendation = data.get("recommendation", "revise")
    if total_verified and not has_issues:
        recommendation = "approve"

    return BudgetAuditResult(
        total_verified=total_verified,
        line_items=line_items,
        flags=flags,
        recommendation=recommendation,
    )


@app.post("/regulatory_watch", response_model=RegulatoryWatchResult, summary="Scan for regulatory changes affecting public health lab RFPs")
async def regulatory_watch(days_back: int = 30) -> RegulatoryWatchResult:
    content = await _azure_openai_chat([
        {"role": "system", "content": (
            "You are a federal regulatory specialist for public health laboratory programs. "
            "Identify recent or upcoming regulatory changes that materially affect public health lab cooperative agreements — "
            "including 2 CFR 200, 45 CFR, CLIA, CDC cooperative agreement requirements, and HHS grant policy. "
            "Focus on changes from the past year that grant writers and RFP authors need to know about. "
            "Return JSON with key 'alerts' containing a list of objects, each with keys: "
            "regulation (str), effective_date (str), change_summary (str), affected_programs (list of str), action_required (str)."
        )},
        {"role": "user", "content": (
            f"List up to 5 regulatory changes relevant to public health laboratory RFPs from the past {days_back} days "
            "or upcoming changes grant writers should prepare for. If none are recent, include the most impactful "
            "standing requirements RFP authors often overlook."
        )},
    ], max_tokens=1500, json_mode=True)

    try:
        data = json.loads(content)
        alerts = [RegulatoryAlert(**a) for a in data.get("alerts", [])]
    except Exception:
        alerts = []
    return RegulatoryWatchResult(
        alerts=alerts,
        checked_at=datetime.utcnow().isoformat() + "Z",
        days_back=days_back,
    )


@app.post("/api/messages", summary="Teams Bot webhook")
async def messages(req: Request) -> Response:
    """Bot Framework webhook — receives all Teams channel activities."""
    print("=== /api/messages HIT ===", flush=True)
    try:
        body = await req.json()
        activity = Activity().deserialize(body)
        auth_header = req.headers.get("Authorization", "")
        log.info("Received activity type=%s from=%s auth=%s",
                 body.get("type"), body.get("from", {}).get("id", "?"),
                 "present" if auth_header else "missing")
        response = await _bot_adapter.process_activity(activity, auth_header, RfpBotHandler().on_turn)
        if response:
            return Response(content=str(response.body), media_type="application/json", status_code=response.status)
        return Response(status_code=201)
    except Exception as exc:
        log.exception("Bot adapter error: %s", exc)
        return Response(status_code=200)  # return 200 so Teams retries cleanly


@app.get("/health", response_model=HealthResponse, summary="Health check")
async def health() -> HealthResponse:
    orchestrator_ok = False
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{ORCHESTRATOR_URL}/health")
            orchestrator_ok = resp.status_code == 200
    except Exception:
        pass

    return HealthResponse(
        status="ok",
        orchestrator_reachable=orchestrator_ok,
        search_reachable=True,
        sharepoint_configured=_sp_client is not None,
        fabric_configured=_fabric_client is not None,
    )
