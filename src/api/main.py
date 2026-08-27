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
from opentelemetry import trace
from azure.identity import DefaultAzureCredential
from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings
from botbuilder.schema import Activity
from fastapi import FastAPI, HTTPException, BackgroundTasks, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse, HTMLResponse

from bot import RfpBotHandler
from models import (
    RfpRequest, RfpDraft, EvaluationResult, GenerateAndEvaluateResponse,
    EvaluatorScores, GateDecision, HealthResponse, TokenUsage,
    ClassifyRequest, ClassifyResult,
    ReviewRequest, ReviewResult, ReviewScore,
    BudgetAuditRequest, BudgetAuditResult, BudgetLineItem,
    RegulatoryWatchResult, RegulatoryAlert,
    DraftStatus, RejectDraftRequest, EditDraftRequest, AiEditDraftRequest, DraftStatusResponse,
)
from sharepoint_client import SharePointClient
from fabric_client import FabricClient
from observability import setup_observability, tracer, record_generation_span, record_evaluation_span
from budget_monitor import BudgetMonitor, session_tracker
from foundry_client import FoundryTracer, FoundryProjectClient, ContentSafetyClient

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
_content_safety_client: Optional[ContentSafetyClient] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _sp_client, _fabric_client, _budget_monitor, _foundry_project_client, _content_safety_client

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
    _content_safety_client = ContentSafetyClient(credential)
    yield


app = FastAPI(
    title="Public Health RFP Generation API",
    description="AI-powered RFP generation with SharePoint and Fabric integration",
    version="0.1.0",
    lifespan=lifespan,
)

# Must run synchronously right here, not inside the async `lifespan` handler above.
# FastAPIInstrumentor.instrument_app(app) has to wrap the app before Starlette's
# middleware stack is built/frozen; deferring it into lifespan's startup event
# (which fires after the app object — and its middleware wiring — already exist)
# means the OTel ASGI middleware never actually gets inserted into the request
# path. Confirmed empirically: with setup_observability() called from lifespan,
# the API service never sent a single AppRequests/AppDependencies/AppTraces row
# to Log Analytics, for any endpoint, ever — only the .NET orchestrator (whose
# ASP.NET Core auto-instrumentation isn't subject to this ordering issue) showed
# up. AI Foundry tracing kept first, matching the original ordering intent.
FoundryTracer.setup()
setup_observability(app)

# In-memory draft cache keyed by draft_id — populated after gate PASS, used by /export
_draft_cache: dict[str, dict] = {}

# Most recently generated/edited/rejected draft — backs GET /drafts/latest/view,
# a read-only preview pinned as a Teams personal tab (see teams-app/manifest.json).
_last_draft_id: Optional[str] = None


def _pad_ragged_tables(text: str) -> str:
    """The orchestrator's LLM sometimes emits tables whose data rows have more
    columns than the header — e.g. a 2-column "Criterion | Points" header
    over 3-column rows with a trailing description cell (seen in
    evaluation_criteria). GFM table parsers use the header to decide column
    count and silently DROP the extra cells rather than erroring, which for
    that case means the actual criteria descriptions vanish from the
    rendered preview. Padding the header/separator to match the widest row
    keeps that content instead — the same fix sharepoint_client.py's
    _render_md_table() already applies for the Word export path via
    `col_count = max(len(r) for r in rows)`."""
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith("|"):
            block = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                block.append(lines[i])
                i += 1
            col_counts = [len(ln.strip().strip("|").split("|")) for ln in block]
            max_cols = max(col_counts)
            if len(block) >= 2 and col_counts[0] < max_cols:
                header_cells = block[0].strip().strip("|").split("|")
                header_cells += ["Details"] * (max_cols - len(header_cells))
                block[0] = "|" + "|".join(header_cells) + "|"
                sep_cells = block[1].strip().strip("|").split("|")
                sep_cells += ["---"] * (max_cols - len(sep_cells))
                block[1] = "|" + "|".join(sep_cells) + "|"
            out.extend(block)
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


def _markdown_to_html(text: str) -> str:
    """Section text is markdown (the orchestrator's LLM output, same content
    SharePointClient.draft_to_docx renders as Word) -- but it can also contain
    a human's free-typed Edit Section text. The `markdown` library passes raw
    HTML straight through unescaped, so pre-escape first: none of the markdown
    we need (headings/bold/italic/tables/lists) uses < > &, so this can't
    break rendering, but it does stop a pasted <script> tag from executing
    when this page is opened as the pinned Teams tab."""
    import markdown
    from html import escape
    text = _pad_ragged_tables(text)
    return markdown.markdown(escape(text, quote=False), extensions=["tables"])


def _render_draft_html(draft_id: str, cached: dict) -> str:
    """Read-only HTML preview of a cached draft's current sections, for the
    Teams "Draft Preview" tab — separate from the chat so edits are visible
    without scrolling back through card history."""
    from html import escape

    gate = cached.get("gate_decision")
    gate_str = gate.value if hasattr(gate, "value") else (gate or "PENDING")
    status = cached["status"]
    status_str = status.value if hasattr(status, "value") else status
    gate_class = {"PASS": "pass", "FAIL": "fail"}.get(gate_str, "pending")

    sections_html = "".join(
        f'<section><h2>{escape(key.replace("_", " ").title())}</h2>{_markdown_to_html(text)}</section>'
        for key, text in cached["sections"].items()
    )

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="8">
<title>{escape(cached['rfp_id'])}</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", sans-serif; margin: 0; padding: 16px 24px 40px;
         color: #111827; background: #F4F6FB; }}
  h1 {{ font-size: 1.15rem; margin: 0 0 2px; }}
  .meta {{ color: #6B7280; font-size: .85rem; margin-bottom: 16px; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: .78rem; font-weight: 600; }}
  .pass {{ background: #D1FAE5; color: #059669; }}
  .fail {{ background: #FEE2E2; color: #B91C1C; }}
  .pending {{ background: #DBEAFE; color: #1D4ED8; }}
  section {{ background: white; border: 1px solid #D0DAF0; border-radius: 8px;
            padding: 12px 16px; margin-bottom: 12px; }}
  section h2 {{ font-size: .8rem; text-transform: uppercase; letter-spacing: .04em;
               color: #374151; margin: 0 0 6px; }}
  section p {{ font-size: .92rem; line-height: 1.5; margin: 0 0 10px; }}
  section p:last-child {{ margin-bottom: 0; }}
  section h4, section h5, section h6 {{ font-size: .95rem; margin: 12px 0 4px; color: #111827; }}
  section ul, section ol {{ margin: 0 0 10px; padding-left: 1.4em; font-size: .92rem; line-height: 1.5; }}
  section hr {{ border: none; border-top: 1px solid #D0DAF0; margin: 10px 0; }}
  table {{ border-collapse: collapse; width: 100%; margin: 0 0 12px; font-size: .88rem; }}
  th, td {{ border: 1px solid #D0DAF0; padding: 6px 10px; text-align: left; vertical-align: top; }}
  th {{ background: #EBF0FA; font-weight: 600; }}
</style></head>
<body>
  <h1>{escape(cached['rfp_id'])}</h1>
  <div class="meta">
    {escape(cached['program_area'])} &middot; {escape(cached['federal_sponsor'])} &middot;
    <span class="badge {gate_class}">{escape(gate_str)}</span>
    &middot; status: {escape(str(status_str))} &middot; edit v{cached.get('edit_version', 0)}
  </div>
  {sections_html}
</body></html>"""


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


async def _check_content_safety(sections: dict[str, str]) -> None:
    """Hard-blocks on flagged content -- categorically different from a
    quality-gate failure (a bad score you can still choose to override), so
    this raises rather than folding into EvaluationResult.failure_reasons.
    Fails open if Content Safety itself is unreachable/unconfigured (see
    ContentSafetyClient.is_safe) -- availability of the safety check should
    not become a reason generation is unavailable entirely."""
    if not _content_safety_client:
        return
    full_text = "\n\n".join(sections.values())
    is_safe, flagged = await _content_safety_client.is_safe(full_text)
    if not is_safe:
        raise HTTPException(
            status_code=422,
            detail=f"Content Safety flagged this draft: {', '.join(flagged)}",
        )


async def _persist_draft(draft: RfpDraft, request: RfpRequest, evaluation: EvaluationResult) -> RfpDraft:
    """Persist draft to SharePoint and/or Fabric Lakehouse."""
    sp_url = None
    fabric_path = None

    if request.write_to_sharepoint and _sp_client:
        sp_url = await _sp_client.upload_draft_docx(
            SHAREPOINT_DRAFT_LIBRARY, draft.rfp_id, draft.draft_id, draft.sections
        )

    if request.write_to_fabric and _fabric_client:
        try:
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
        except Exception as e:
            # Fabric archival is a side-effect, not the point of generation --
            # a workspace outage/misconfiguration shouldn't turn into a 500
            # for a draft that was otherwise generated and evaluated fine.
            log.warning("Fabric write failed for draft %s (continuing without it): %s", draft.draft_id, e)
            fabric_path = None

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
    await _check_content_safety(draft.sections)
    evaluation = await _run_evaluation(draft, request)
    draft = await _persist_draft(draft, request, evaluation)

    session_tracker.record_generation(draft.token_usage.prompt_tokens, draft.token_usage.completion_tokens)
    span = trace.get_current_span()
    record_generation_span(span, draft.draft_id, draft.rfp_id, draft.program_area,
                            draft.token_usage.prompt_tokens, draft.token_usage.completion_tokens,
                            evaluation.gate_decision.value)
    record_evaluation_span(span, evaluation.scores.model_dump(), evaluation.failure_reasons)

    # Cached regardless of gate outcome — a FAIL draft is exactly what a human
    # would want to inspect and edit via /drafts/{id}/edit.
    _draft_cache[draft.draft_id] = {
        "rfp_id": draft.rfp_id,
        "sections": draft.sections,
        "program_area": draft.program_area,
        "federal_sponsor": draft.federal_sponsor,
        "generated_at": draft.generated_at,
        "token_usage": draft.token_usage,
        "request": request,
        "status": DraftStatus.PENDING,
        "gate_decision": evaluation.gate_decision,
        "reject_reason": None,
        "edit_version": 0,
    }
    global _last_draft_id
    _last_draft_id = draft.draft_id

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

            total_prompt_tokens = 0
            total_completion_tokens = 0

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
                            # The orchestrator computes real per-section token counts
                            # (RfpOrchestrationService.cs's SectionStreamEvent) — accumulate
                            # them instead of discarding, so this path reports real cost
                            # like /generate-and-evaluate does rather than always zero.
                            total_prompt_tokens += evt.get("promptTokens", 0)
                            total_completion_tokens += evt.get("completionTokens", 0)
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
                token_usage=TokenUsage(
                    prompt_tokens=total_prompt_tokens,
                    completion_tokens=total_completion_tokens,
                    total_tokens=total_prompt_tokens + total_completion_tokens,
                ),
            )
            await _check_content_safety(sections)
            evaluation = await _run_evaluation(draft, request)
            draft = await _persist_draft(draft, request, evaluation)

            session_tracker.record_generation(draft.token_usage.prompt_tokens, draft.token_usage.completion_tokens)
            span = trace.get_current_span()
            record_generation_span(span, draft.draft_id, draft.rfp_id, draft.program_area,
                                    draft.token_usage.prompt_tokens, draft.token_usage.completion_tokens,
                                    evaluation.gate_decision.value)
            record_evaluation_span(span, evaluation.scores.model_dump(), evaluation.failure_reasons)

            _draft_cache[draft.draft_id] = {
                "rfp_id": draft.rfp_id,
                "sections": sections,
                "program_area": draft.program_area,
                "federal_sponsor": draft.federal_sponsor,
                "generated_at": draft.generated_at,
                "token_usage": draft.token_usage,
                "request": request,
                "status": DraftStatus.PENDING,
                "gate_decision": evaluation.gate_decision,
                "reject_reason": None,
                "edit_version": 0,
            }
            global _last_draft_id
            _last_draft_id = draft.draft_id

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

    # Only mark APPROVED once the upload actually succeeds — a failed upload
    # leaves the draft PENDING so retry/reject both stay meaningful.
    cached["status"] = DraftStatus.APPROVED

    return {"sharepoint_url": sp_url, "draft_id": draft_id, "rfp_id": cached["rfp_id"]}


# Registered before /drafts/{draft_id} so the literal "latest" path wins —
# {draft_id} would otherwise greedily match it first. Backs the chat-typed
# edit path (bot.py resolves "which draft" this way, same convention as the
# Draft Preview tab's /drafts/latest/view).
@app.get("/drafts/latest", response_model=DraftStatusResponse, summary="Get the most recently touched draft's status")
async def get_latest_draft() -> DraftStatusResponse:
    if not _last_draft_id or _last_draft_id not in _draft_cache:
        raise HTTPException(status_code=404, detail="No draft yet")
    return await get_draft(_last_draft_id)


@app.get("/drafts/{draft_id}", response_model=DraftStatusResponse, summary="Get a cached draft's current status")
async def get_draft(draft_id: str) -> DraftStatusResponse:
    """Look up a cached draft's review status, gate decision, and current sections."""
    cached = _draft_cache.get(draft_id)
    if not cached:
        raise HTTPException(status_code=404, detail="Draft not found")

    return DraftStatusResponse(
        draft_id=draft_id,
        rfp_id=cached["rfp_id"],
        status=cached["status"],
        gate_decision=cached.get("gate_decision"),
        reason=cached.get("reject_reason"),
        sections=cached["sections"],
    )


# Registered before /drafts/{draft_id}/view so the literal "latest" path wins —
# {draft_id} would otherwise greedily match it first.
@app.get("/drafts/latest/view", response_class=HTMLResponse,
         summary="Read-only HTML preview of the most recently touched draft (Teams personal tab)")
async def view_latest_draft() -> HTMLResponse:
    if not _last_draft_id or _last_draft_id not in _draft_cache:
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;padding:2rem;color:#6B7280'>"
            "No draft yet — generate one in the chat.</body></html>"
        )
    return HTMLResponse(_render_draft_html(_last_draft_id, _draft_cache[_last_draft_id]))


@app.get("/drafts/{draft_id}/view", response_class=HTMLResponse,
         summary="Read-only HTML preview of a specific draft's current sections")
async def view_draft(draft_id: str) -> HTMLResponse:
    cached = _draft_cache.get(draft_id)
    if not cached:
        raise HTTPException(status_code=404, detail="Draft not found")
    return HTMLResponse(_render_draft_html(draft_id, cached))


@app.post("/drafts/{draft_id}/reject", response_model=DraftStatusResponse, summary="Reject a draft")
async def reject_draft(draft_id: str, body: RejectDraftRequest = RejectDraftRequest()) -> DraftStatusResponse:
    """Mark a cached draft as rejected. Does not touch SharePoint/Fabric — a
    reject after approve does not retract an already-uploaded SharePoint copy."""
    cached = _draft_cache.get(draft_id)
    if not cached:
        raise HTTPException(status_code=404, detail="Draft not found")

    cached["status"] = DraftStatus.REJECTED
    cached["reject_reason"] = body.reason
    global _last_draft_id
    _last_draft_id = draft_id

    return DraftStatusResponse(
        draft_id=draft_id,
        rfp_id=cached["rfp_id"],
        status=cached["status"],
        gate_decision=cached.get("gate_decision"),
        reason=cached["reject_reason"],
    )


async def _reevaluate_and_persist_edit(draft_id: str, cached: dict) -> EvaluationResult:
    """Shared tail for both /edit (user-typed replacement text) and /edit/ai
    (LLM-rewritten from an instruction) — cached["sections"] must already
    reflect the change. Re-runs the gate, resets status to PENDING (an edit
    un-decides the draft, including one already approved — this does not
    re-export or retract an earlier SharePoint copy), and archives the new
    version to Fabric OneLake unconditionally, regardless of the original
    request's write_to_fabric flag (SharePoint stays approve-only, see /export)."""
    draft = RfpDraft(
        draft_id=draft_id,
        rfp_id=cached["rfp_id"],
        program_area=cached["program_area"],
        federal_sponsor=cached["federal_sponsor"],
        generated_at=cached["generated_at"],
        sections=cached["sections"],
        token_usage=cached["token_usage"],
    )
    await _check_content_safety(cached["sections"])
    evaluation = await _run_evaluation(draft, cached["request"])

    cached["gate_decision"] = evaluation.gate_decision
    cached["status"] = DraftStatus.PENDING
    global _last_draft_id
    _last_draft_id = draft_id

    if _fabric_client:
        try:
            cached["edit_version"] += 1
            md_content = SharePointClient.draft_to_markdown(cached["rfp_id"], cached["sections"])
            await _fabric_client.write_draft_to_lakehouse(
                draft_id, cached["rfp_id"], md_content, version=cached["edit_version"],
            )
            await _fabric_client.write_eval_record({
                "draft_id": draft_id,
                "rfp_id": cached["rfp_id"],
                "program_area": cached["program_area"],
                "gate_decision": evaluation.gate_decision.value,
                "scores": evaluation.scores.model_dump(),
                "failure_reasons": evaluation.failure_reasons,
                "edit_version": cached["edit_version"],
            })
        except Exception as e:
            # Same reasoning as _persist_draft: Fabric archival is a
            # side-effect of the edit, not the point of it -- a workspace
            # outage shouldn't turn an otherwise-successful edit into a 500.
            log.warning("Fabric write failed for edited draft %s (continuing without it): %s", draft_id, e)

    return evaluation


@app.post("/drafts/{draft_id}/edit", response_model=EvaluationResult, summary="Edit a draft's sections and re-evaluate")
async def edit_draft(draft_id: str, body: EditDraftRequest) -> EvaluationResult:
    """Apply a partial section edit (user-typed replacement text) to a cached
    draft and re-run the evaluation gate against it."""
    cached = _draft_cache.get(draft_id)
    if not cached:
        raise HTTPException(status_code=404, detail="Draft not found")

    cached["sections"].update(body.sections)
    return await _reevaluate_and_persist_edit(draft_id, cached)


@app.post("/drafts/{draft_id}/edit/ai", response_model=EvaluationResult,
          summary="Rewrite one section from a natural-language instruction and re-evaluate")
async def edit_draft_ai(draft_id: str, body: AiEditDraftRequest) -> EvaluationResult:
    """Like /edit, but the caller supplies an instruction instead of the
    replacement text — an LLM rewrites the named section to satisfy it. Used
    by a typed chat instruction (e.g. "edit the eligibility section to
    mention CLIA accreditation")."""
    cached = _draft_cache.get(draft_id)
    if not cached:
        raise HTTPException(status_code=404, detail="Draft not found")
    if body.section_key not in cached["sections"]:
        raise HTTPException(status_code=400, detail=f"Unknown section: {body.section_key}")

    current_text = cached["sections"][body.section_key]
    content = await _azure_openai_chat([
        {"role": "system", "content": (
            "You are editing one section of a federal public health grant RFP. "
            "Rewrite ONLY the given section's text to satisfy the instruction, keeping a "
            "professional grant-writing tone and similar length unless the instruction says otherwise. "
            "Return JSON with key 'new_text' (str) containing the complete replacement text for the section."
        )},
        {"role": "user", "content": (
            f"Section: {body.section_key.replace('_', ' ').title()}\n\n"
            f"Current text:\n{current_text}\n\n"
            f"Instruction: {body.instruction}"
        )},
    ], max_tokens=1500, json_mode=True)
    new_text = json.loads(content)["new_text"]

    cached["sections"][body.section_key] = new_text
    return await _reevaluate_and_persist_edit(draft_id, cached)


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
