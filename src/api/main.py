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
    EvaluatorScores, GateDecision, HealthResponse, TokenUsage
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
    sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent.parent / "evaluation"))
    from gate import evaluate_draft

    full_text = "\n\n".join(draft.sections.values())
    params = {
        "total_funding": request.total_funding,
        "period_of_performance_months": request.period_of_performance_months,
        "cost_sharing_required": request.cost_sharing_required,
        "award_range_min": request.award_range_min,
        "award_range_max": request.award_range_max,
    }

    decision = evaluate_draft(
        draft_text=full_text,
        sections=draft.sections,
        expected_params=params,
    )

    return EvaluationResult(
        draft_id=draft.draft_id,
        rfp_id=draft.rfp_id,
        gate_decision=GateDecision.PASS if decision.passed else GateDecision.FAIL,
        scores=EvaluatorScores(
            completeness=decision.scores.get("completeness", 0.0),
            parameter_accuracy=decision.scores.get("parameter_accuracy", 0.0),
            compliance=decision.scores.get("compliance", 0.0),
            groundedness=decision.scores.get("groundedness", 0.0),
            coherence=decision.scores.get("coherence", 0.0),
        ),
        failure_reasons=decision.failure_reasons,
        passed_threshold=decision.passed_threshold,
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
                sections=sections,
                grounding_chunks=[],
                token_usage=TokenUsage(prompt_tokens=0, completion_tokens=0),
            )
            evaluation = await _run_evaluation(draft, request)
            draft = await _persist_draft(draft, request, evaluation)

            yield json.dumps({
                "type": "gate_result",
                "passed": evaluation.gate_decision.value == "pass",
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


@app.post("/api/messages", summary="Teams Bot webhook")
async def messages(req: Request) -> Response:
    """Bot Framework webhook — receives all Teams channel activities."""
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
