"""
Teams Bot — RFP Generation ActivityHandler.

Receives messages via Azure Bot Service webhook (/api/messages).
For generation requests, sends an initial Adaptive Card progress
display and updates it in-place as each of the 8 RFP sections
completes, then shows the final gate result with a SharePoint link.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import urllib.parse
from typing import Any

import httpx
from botbuilder.core import ActivityHandler, CardFactory, TurnContext
from botbuilder.schema import Activity, ActivityTypes

APP_ID = os.getenv("MICROSOFT_APP_ID", "")
APP_PASSWORD = os.getenv("MICROSOFT_APP_PASSWORD", "")

log = logging.getLogger(__name__)

API_URL = os.getenv("API_URL", "http://localhost:8000")
# API_URL above is the container-internal address (bot.py -> main.py, same container).
# Card buttons open in the user's own client, so they need the externally reachable
# address instead — set by scripts/post-deploy.sh after every deploy, since the FQDN
# isn't known until then and changes on every fresh re-provision.
PUBLIC_API_URL = os.getenv("PUBLIC_API_URL", API_URL)

# entityId of the personal staticTab declared in teams-app/manifest.json.
DRAFT_PREVIEW_ENTITY_ID = "draftPreview"


def _draft_preview_url(draft_id: str) -> str:
    """Teams entity deep link — switches to the pinned Draft Preview tab
    inside the Teams client instead of opening a browser. The tab itself is a
    *static* tab (fixed contentUrl, /drafts/latest/view) so this always shows
    whatever draft was most recently touched, not necessarily draft_id — the
    right tradeoff for a card you just generated/edited, since that draft
    _is_ "latest" at the moment the card is built. webUrl is the fallback for
    clients where the app/tab isn't recognized (e.g. Teams web without the
    app installed)."""
    fallback = f"{PUBLIC_API_URL}/drafts/{draft_id}/view"
    return (
        f"https://teams.microsoft.com/l/entity/{APP_ID}/{DRAFT_PREVIEW_ENTITY_ID}"
        f"?webUrl={urllib.parse.quote(fallback, safe='')}"
        f"&label={urllib.parse.quote('Draft Preview')}"
    )

SECTION_DISPLAY = {
    "background":             "Background and Purpose",
    "funding_parameters":     "Funding Parameters",
    "eligibility":            "Eligibility Criteria",
    "scope_of_work":          "Scope of Work",
    "reporting_requirements": "Reporting Requirements",
    "budget_requirements":    "Budget Requirements",
    "evaluation_criteria":    "Evaluation Criteria",
    "submission_instructions":"Submission Instructions",
}
SECTION_ORDER = list(SECTION_DISPLAY.keys())


# ── Adaptive Card builders ──────────────────────────────────────────────────────

def _progress_card(completed: list[str], in_progress: str | None, subtitle: str) -> dict[str, Any]:
    rows = []
    for key in SECTION_ORDER:
        if key in completed:
            icon, color, subtle = "✓", "Good", False
        elif key == in_progress:
            icon, color, subtle = "⏳", "Accent", False
        else:
            icon, color, subtle = "○", "Default", True
        rows.append({
            "type": "ColumnSet",
            "spacing": "None",
            "columns": [
                {"type": "Column", "width": "auto", "items": [
                    {"type": "TextBlock", "text": icon, "color": color, "size": "Small"}
                ]},
                {"type": "Column", "width": "stretch", "items": [
                    {"type": "TextBlock", "text": SECTION_DISPLAY[key],
                     "color": color, "size": "Small", "isSubtle": subtle}
                ]},
            ]
        })

    done = len(completed)
    total = len(SECTION_ORDER)
    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.5",
        "body": [
            {"type": "TextBlock", "text": "Generating RFP Draft",
             "weight": "Bolder", "size": "Medium"},
            {"type": "TextBlock", "text": subtitle,
             "isSubtle": True, "size": "Small", "spacing": "None"},
            {"type": "TextBlock",
             "text": f"{done} of {total} sections complete" if done < total else "Running evaluation gate…",
             "color": "Accent", "size": "Small", "spacing": "Small"},
            {"type": "Container", "items": rows, "spacing": "Small"},
        ]
    }


def _result_card(event: dict, subtitle: str) -> dict[str, Any]:
    passed = event.get("passed", False)
    scores = event.get("scores", {})
    sp_url = event.get("sharepoint_url", "")
    failure_reasons = event.get("failure_reasons", [])
    rfp_id = event.get("rfp_id", "")

    THRESHOLDS = {
        "completeness": 0.80, "parameter_accuracy": 1.00,
        "compliance": 0.80, "groundedness": 0.80, "coherence": 0.75,
    }
    DIM_LABELS = {
        "completeness": "Completeness", "parameter_accuracy": "Parameter Accuracy",
        "compliance": "Compliance", "groundedness": "Groundedness", "coherence": "Coherence",
    }

    score_rows = []
    for dim, label in DIM_LABELS.items():
        val = scores.get(dim, 0.0)
        ok = val >= THRESHOLDS[dim]
        score_rows.append({
            "type": "ColumnSet", "spacing": "None",
            "columns": [
                {"type": "Column", "width": "stretch", "items": [
                    {"type": "TextBlock", "text": label, "size": "Small"}
                ]},
                {"type": "Column", "width": "auto", "items": [
                    {"type": "TextBlock", "text": f"{val:.2f}",
                     "color": "Good" if ok else "Attention",
                     "size": "Small", "horizontalAlignment": "Right"}
                ]},
            ]
        })

    body: list[dict] = [
        {"type": "TextBlock",
         "text": "✓ Draft Ready — Gate Passed" if passed else "✗ Gate Failed — Revisions Required",
         "weight": "Bolder",
         "color": "Good" if passed else "Attention",
         "size": "Medium"},
        {"type": "TextBlock", "text": subtitle,
         "isSubtle": True, "size": "Small", "spacing": "None"},
    ]
    if rfp_id:
        body.append({"type": "TextBlock", "text": f"RFP ID: {rfp_id}",
                     "size": "Small", "spacing": "None"})
    body += [
        {"type": "TextBlock", "text": "Evaluation Scores",
         "weight": "Bolder", "size": "Small", "spacing": "Medium"},
        *score_rows,
    ]
    if failure_reasons:
        body.append({
            "type": "TextBlock",
            "text": "Issues: " + "; ".join(failure_reasons),
            "color": "Attention", "size": "Small", "wrap": True, "spacing": "Small",
        })

    edited_key = event.get("edited_section_key")
    if edited_key:
        body.append({"type": "TextBlock", "text": f"Updated: {edited_key.replace('_', ' ').title()}",
                     "isSubtle": True, "size": "Small", "spacing": "Small"})

    draft_id = event.get("draft_id", "")
    sections = event.get("sections", {})

    actions = []
    if draft_id:
        actions.append({
            "type": "Action.OpenUrl",
            "title": "📄 Open Draft Preview",
            "url": _draft_preview_url(draft_id),
        })
    if passed and sp_url:
        actions.append({
            "type": "Action.OpenUrl",
            "title": "Open Word Draft in SharePoint",
            "url": sp_url,
        })
    elif passed and draft_id:
        actions.append({
            "type": "Action.Submit",
            "title": "✅ Approve & Save to SharePoint",
            "data": {"action": "approve_rfp", "draft_id": draft_id},
        })

    # Reject/Edit are offered regardless of gate pass/fail — a FAILED draft is
    # exactly the case where editing a section to fix it matters most.
    if draft_id:
        actions.append({
            "type": "Action.Submit",
            "title": "❌ Reject",
            "data": {"action": "reject_rfp", "draft_id": draft_id},
        })
        if sections:
            actions.append({
                "type": "Action.ShowCard",
                "title": "✏️ Edit Section",
                "card": {
                    "type": "AdaptiveCard",
                    "body": [
                        {
                            "type": "Input.ChoiceSet",
                            "id": "section_key",
                            "label": "Section",
                            "value": next(iter(sections)),
                            "choices": [
                                {"title": key.replace("_", " ").title(), "value": key}
                                for key in sections
                            ],
                        },
                        {
                            "type": "Input.Text",
                            "id": "new_text",
                            "label": "New text",
                            "isMultiline": True,
                        },
                    ],
                    "actions": [{
                        "type": "Action.Submit",
                        "title": "Submit Edit",
                        "data": {"action": "edit_rfp_section", "draft_id": draft_id},
                    }],
                },
            })
            actions.append({
                "type": "Action.ShowCard",
                "title": "🤖 AI Edit",
                "card": {
                    "type": "AdaptiveCard",
                    "body": [
                        {
                            "type": "Input.ChoiceSet",
                            "id": "section_key",
                            "label": "Section",
                            "value": next(iter(sections)),
                            "choices": [
                                {"title": key.replace("_", " ").title(), "value": key}
                                for key in sections
                            ],
                        },
                        {
                            "type": "Input.Text",
                            "id": "instruction",
                            "label": "What should change?",
                            "isMultiline": True,
                            "placeholder": "e.g. add a mention of CLIA accreditation requirements",
                        },
                    ],
                    "actions": [{
                        "type": "Action.Submit",
                        "title": "Rewrite Section",
                        "data": {"action": "ai_edit_rfp_section", "draft_id": draft_id},
                    }],
                },
            })

    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.5",
        "body": body,
        "actions": actions,
    }


# ── Intent detection ────────────────────────────────────────────────────────────

_INTENT_PATTERNS = [
    (re.compile(r"\b(generate|draft|write|create).{0,30}rfp\b", re.I), "generate_rfp"),
    (re.compile(r"\b(review|score|evaluate).{0,30}proposal\b", re.I), "review_proposal"),
    (re.compile(r"\b(classify|categorize|what program)\b", re.I), "classify"),
    (re.compile(r"\b(regulatory|federal register|cfr|guidance change)\b", re.I), "regulatory_watch"),
    (re.compile(r"\b(budget|allowable|indirect cost|cost principle)\b", re.I), "audit_budget"),
]

def _detect_intent(text: str) -> str | None:
    for pattern, intent in _INTENT_PATTERNS:
        if pattern.search(text):
            return intent
    return None


# ── Capability response cards ────────────────────────────────────────────────────

def _classify_card(result: dict, subtitle: str) -> dict:
    confidence = result.get("confidence", 0.0)
    pct = int(confidence * 100)
    color = "Good" if confidence >= 0.80 else ("Warning" if confidence >= 0.60 else "Attention")
    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard", "version": "1.5",
        "body": [
            {"type": "TextBlock", "text": "Program Area Classification",
             "weight": "Bolder", "size": "Medium"},
            {"type": "TextBlock", "text": subtitle, "isSubtle": True, "size": "Small", "spacing": "None"},
            {"type": "TextBlock", "text": result.get("program_area", ""), "size": "Large",
             "weight": "Bolder", "color": color, "spacing": "Medium"},
            {"type": "ColumnSet", "spacing": "None", "columns": [
                {"type": "Column", "width": "stretch", "items": [
                    {"type": "TextBlock", "text": "Confidence", "size": "Small", "isSubtle": True}]},
                {"type": "Column", "width": "auto", "items": [
                    {"type": "TextBlock", "text": f"{pct}%", "color": color,
                     "size": "Small", "horizontalAlignment": "Right"}]},
            ]},
            {"type": "TextBlock", "text": result.get("rationale", ""), "wrap": True,
             "size": "Small", "isSubtle": True, "spacing": "Small"},
        ],
    }


def _review_card(result: dict, subtitle: str) -> dict:
    rec = result.get("recommendation", "revise")
    rec_color = {"fund": "Good", "revise": "Warning", "reject": "Attention"}.get(rec, "Default")
    scores = result.get("scores", {})
    score_rows = []
    for criterion, data in scores.items():
        score = data.get("score", 0)
        ok = score >= 14
        score_rows.append({
            "type": "ColumnSet", "spacing": "None",
            "columns": [
                {"type": "Column", "width": "stretch", "items": [
                    {"type": "TextBlock", "text": criterion, "size": "Small"}]},
                {"type": "Column", "width": "auto", "items": [
                    {"type": "TextBlock", "text": f"{score}/20",
                     "color": "Good" if ok else "Attention",
                     "size": "Small", "horizontalAlignment": "Right"}]},
            ]
        })
    body = [
        {"type": "TextBlock", "text": "Proposal Review", "weight": "Bolder", "size": "Medium"},
        {"type": "TextBlock", "text": subtitle, "isSubtle": True, "size": "Small", "spacing": "None"},
        {"type": "ColumnSet", "spacing": "Small", "columns": [
            {"type": "Column", "width": "stretch", "items": [
                {"type": "TextBlock", "text": f"Total: {result.get('total_score', 0)}/100",
                 "weight": "Bolder", "size": "Large"}]},
            {"type": "Column", "width": "auto", "items": [
                {"type": "TextBlock", "text": rec.upper(),
                 "color": rec_color, "weight": "Bolder", "size": "Large",
                 "horizontalAlignment": "Right"}]},
        ]},
        {"type": "TextBlock", "text": "Criterion Scores",
         "weight": "Bolder", "size": "Small", "spacing": "Medium"},
        *score_rows,
    ]
    if result.get("flags"):
        body.append({"type": "TextBlock",
                     "text": "Flags: " + "; ".join(result["flags"]),
                     "color": "Attention", "size": "Small", "wrap": True, "spacing": "Small"})
    return {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard", "version": "1.5", "body": body}


def _budget_card(result: dict, subtitle: str) -> dict:
    rec = result.get("recommendation", "revise")
    rec_color = {"approve": "Good", "revise": "Warning", "reject": "Attention"}.get(rec, "Default")
    verified = result.get("total_verified", False)
    line_rows = []
    for li in result.get("line_items", [])[:10]:
        ok = li.get("allowable", True)
        line_rows.append({
            "type": "ColumnSet", "spacing": "None",
            "columns": [
                {"type": "Column", "width": "stretch", "items": [
                    {"type": "TextBlock", "text": li.get("item", ""), "size": "Small", "wrap": True}]},
                {"type": "Column", "width": "auto", "items": [
                    {"type": "TextBlock", "text": "✓" if ok else "✗",
                     "color": "Good" if ok else "Attention",
                     "size": "Small", "horizontalAlignment": "Right"}]},
            ]
        })
    body = [
        {"type": "TextBlock", "text": "Budget Audit", "weight": "Bolder", "size": "Medium"},
        {"type": "TextBlock", "text": subtitle, "isSubtle": True, "size": "Small", "spacing": "None"},
        {"type": "ColumnSet", "spacing": "Small", "columns": [
            {"type": "Column", "width": "stretch", "items": [
                {"type": "TextBlock", "text": "Arithmetic Verified" if verified else "Arithmetic Error",
                 "color": "Good" if verified else "Attention", "weight": "Bolder"}]},
            {"type": "Column", "width": "auto", "items": [
                {"type": "TextBlock", "text": rec.upper(),
                 "color": rec_color, "weight": "Bolder", "horizontalAlignment": "Right"}]},
        ]},
    ]
    if line_rows:
        body += [{"type": "TextBlock", "text": "Line Items",
                  "weight": "Bolder", "size": "Small", "spacing": "Medium"}, *line_rows]
    if result.get("flags"):
        body.append({"type": "TextBlock",
                     "text": "Flags: " + "; ".join(result["flags"]),
                     "color": "Attention", "size": "Small", "wrap": True, "spacing": "Small"})
    return {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard", "version": "1.5", "body": body}


def _regulatory_card(result: dict) -> dict:
    alerts = result.get("alerts", [])
    checked = result.get("checked_at", "")[:10]
    alert_blocks = []
    for alert in alerts:
        alert_blocks += [
            {"type": "TextBlock",
             "text": f"📋 {alert.get('regulation', '')} — {alert.get('effective_date', '')}",
             "weight": "Bolder", "size": "Small", "spacing": "Medium"},
            {"type": "TextBlock", "text": alert.get("change_summary", ""),
             "wrap": True, "size": "Small"},
            {"type": "TextBlock",
             "text": f"Action: {alert.get('action_required', '')}",
             "color": "Attention", "size": "Small", "wrap": True, "spacing": "None"},
        ]
    if not alerts:
        alert_blocks.append({"type": "TextBlock",
                              "text": "No material changes found for public health laboratory programs.",
                              "isSubtle": True, "size": "Small"})
    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard", "version": "1.5",
        "body": [
            {"type": "TextBlock", "text": "Regulatory Watch",
             "weight": "Bolder", "size": "Medium"},
            {"type": "TextBlock", "text": f"Federal Register scan as of {checked}",
             "isSubtle": True, "size": "Small", "spacing": "None"},
            *alert_blocks,
        ],
    }


# ── Capability handlers ──────────────────────────────────────────────────────────

async def _handle_classify(turn_context: TurnContext, text: str) -> None:
    await turn_context.send_activity("Classifying program area…")
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(f"{API_URL}/classify", json={"text": text})
        resp.raise_for_status()
        result = resp.json()
    card = _classify_card(result, text[:60])
    await turn_context.send_activity(
        Activity(type=ActivityTypes.message, attachments=[CardFactory.adaptive_card(card)])
    )


async def _get_bot_token() -> str:
    """Get a Bot Framework auth token for downloading Teams file attachments."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": APP_ID,
                "client_secret": APP_PASSWORD,
                "scope": "https://api.botframework.com/.default",
            },
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


async def _extract_attachment_text(download_url: str, content_type: str, name: str,
                                    needs_auth: bool = True) -> str:
    """Download a Teams file attachment and extract its text content.

    needs_auth=False for Teams channel file-share cards whose downloadUrl is pre-authenticated.
    needs_auth=True for personal-chat contentUrl which requires a Bot Framework bearer token.
    """
    headers = {}
    if needs_auth:
        token = await _get_bot_token()
        headers["Authorization"] = f"Bearer {token}"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(download_url, headers=headers, follow_redirects=True)
        resp.raise_for_status()
        data = resp.content

    ct = (content_type or "").lower()
    fname = (name or "").lower()

    if ct == "application/pdf" or fname.endswith(".pdf"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages)

    if "wordprocessingml" in ct or fname.endswith(".docx"):
        from docx import Document
        doc = Document(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs)

    return data.decode("utf-8", errors="replace")


async def _handle_review(turn_context: TurnContext, text: str) -> None:
    await turn_context.send_activity("Reviewing proposal — this may take a moment…")
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(f"{API_URL}/review", json={"proposal_text": text})
        resp.raise_for_status()
        result = resp.json()
    card = _review_card(result, f"Recommendation: {result.get('recommendation', '').upper()}")
    await turn_context.send_activity(
        Activity(type=ActivityTypes.message, attachments=[CardFactory.adaptive_card(card)])
    )


async def _handle_budget(turn_context: TurnContext, text: str) -> None:
    # Match "$2,500,000", "$2.5m", "$2500k" — pick the largest number found (the total)
    amount = 0.0
    for m in re.finditer(r"\$([\d,]+(?:\.\d+)?)\s*([mk]?)\b", text, re.IGNORECASE):
        val = float(m.group(1).replace(",", ""))
        unit = m.group(2).lower()
        if unit == "m":
            val *= 1_000_000
        elif unit == "k":
            val *= 1_000
        if val > amount:
            amount = val
    await turn_context.send_activity("Auditing budget narrative…")
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(f"{API_URL}/audit_budget",
                                 json={"budget_text": text, "total_funding": amount or 500_000})
        resp.raise_for_status()
        result = resp.json()
    card = _budget_card(result, f"${amount:,.0f} award" if amount else "Budget audit")
    await turn_context.send_activity(
        Activity(type=ActivityTypes.message, attachments=[CardFactory.adaptive_card(card)])
    )


async def _handle_regulatory(turn_context: TurnContext) -> None:
    await turn_context.send_activity("Scanning for regulatory changes affecting public health lab RFPs…")
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(f"{API_URL}/regulatory_watch", params={"days_back": 30})
            resp.raise_for_status()
            result = resp.json()
        card = _regulatory_card(result)
        await turn_context.send_activity(
            Activity(type=ActivityTypes.message, attachments=[CardFactory.adaptive_card(card)])
        )
    except Exception as e:
        await turn_context.send_activity(f"Regulatory watch error: {e}")


async def _handle_chat_section_edit(turn_context: TurnContext, section_key: str, instruction: str) -> None:
    """Typed-instruction path — resolves 'which draft' to whichever was most
    recently generated/edited/rejected (same convention as the Draft Preview
    tab's /drafts/latest/view), since a plain chat message carries no
    draft_id the way a card submission does."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{API_URL}/drafts/latest")
            if resp.status_code == 404:
                await turn_context.send_activity("No draft to edit yet — generate one first.")
                return
            resp.raise_for_status()
            draft_id = resp.json()["draft_id"]
    except Exception as e:
        await turn_context.send_activity(f"Could not look up the current draft: {e}")
        return
    await _apply_ai_edit(turn_context, draft_id, section_key, instruction)


# ── Bot handler ─────────────────────────────────────────────────────────────────

async def _apply_ai_edit(turn_context: TurnContext, draft_id: str, section_key: str, instruction: str) -> None:
    """Shared by the card's 'AI Edit' action and a typed chat instruction —
    rewrites one section from a natural-language instruction via
    POST /drafts/{id}/edit/ai, then sends the same result card the manual
    Edit Section flow uses."""
    await turn_context.send_activity("Rewriting section and re-evaluating…")
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{API_URL}/drafts/{draft_id}/edit/ai",
                json={"section_key": section_key, "instruction": instruction},
            )
            if resp.status_code == 404:
                await turn_context.send_activity("Draft not found — it may have expired.")
                return
            if resp.status_code == 400:
                await turn_context.send_activity(resp.json().get("detail", "Unknown section."))
                return
            resp.raise_for_status()
            evaluation = resp.json()

            draft_resp = await client.get(f"{API_URL}/drafts/{draft_id}")
            draft_resp.raise_for_status()
            draft_state = draft_resp.json()

        event = {
            "passed": evaluation["gate_decision"] == "PASS",
            "scores": evaluation["scores"],
            "failure_reasons": evaluation["failure_reasons"],
            "sharepoint_url": "",
            "draft_id": draft_id,
            "rfp_id": evaluation["rfp_id"],
            "sections": draft_state.get("sections", {}),
            "edited_section_key": section_key,
        }
        subtitle = f"AI-edited: {section_key.replace('_', ' ').title()}"
        card = _result_card(event, subtitle)
        await turn_context.send_activity(
            Activity(type=ActivityTypes.message, attachments=[CardFactory.adaptive_card(card)])
        )
    except Exception as e:
        await turn_context.send_activity(f"AI edit failed: {e}")


def _extract_section_edit(text: str) -> str | None:
    """If text reads as a natural-language edit instruction naming a known
    section (e.g. "edit the eligibility section to mention CLIA
    accreditation"), returns that section_key. Requires both an edit verb
    and a section reference so an unrelated message can't accidentally
    trigger this — e.g. "audit this budget" has no edit verb, "give me an
    update" has no section reference."""
    if not re.search(r"\b(edit|change|update|revise|rewrite|modify)\b", text, re.I):
        return None
    lower = text.lower()
    for key in SECTION_ORDER:
        phrase = key.replace("_", " ")
        first_word = phrase.split()[0]
        if phrase in lower or re.search(rf"\b{re.escape(first_word)}\b", lower):
            return key
    return None


class RfpBotHandler(ActivityHandler):
    async def on_message_activity(self, turn_context: TurnContext):
        # Handle Adaptive Card button submissions (e.g., Approve & Save to SharePoint)
        value = turn_context.activity.value or {}
        if isinstance(value, dict) and value.get("action") == "approve_rfp":
            draft_id = value.get("draft_id", "")
            await turn_context.send_activity("Uploading to SharePoint…")
            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.post(f"{API_URL}/export/{draft_id}")
                    if resp.status_code == 404:
                        await turn_context.send_activity("Draft not found — it may have expired. Please generate a new RFP.")
                        return
                    resp.raise_for_status()
                    result = resp.json()
                sp_url = result.get("sharepoint_url", "")
                rfp_id = result.get("rfp_id", draft_id)
                msg = f"✅ **Saved to SharePoint**\n\nRFP **{rfp_id}** is now in the *Generated Drafts* library.\n\n[Open in SharePoint]({sp_url})"
                await turn_context.send_activity(Activity(type=ActivityTypes.message, text=msg))
            except Exception as e:
                await turn_context.send_activity(f"SharePoint upload failed: {e}")
            return

        if isinstance(value, dict) and value.get("action") == "reject_rfp":
            draft_id = value.get("draft_id", "")
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.post(f"{API_URL}/drafts/{draft_id}/reject", json={})
                    if resp.status_code == 404:
                        await turn_context.send_activity("Draft not found — it may have expired.")
                        return
                    resp.raise_for_status()
                await turn_context.send_activity("❌ Draft rejected.")
            except Exception as e:
                await turn_context.send_activity(f"Reject failed: {e}")
            return

        if isinstance(value, dict) and value.get("action") == "edit_rfp_section":
            draft_id = value.get("draft_id", "")
            section_key = value.get("section_key", "")
            new_text = value.get("new_text", "")
            await turn_context.send_activity("Applying edit and re-evaluating…")
            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.post(
                        f"{API_URL}/drafts/{draft_id}/edit",
                        json={"sections": {section_key: new_text}},
                    )
                    if resp.status_code == 404:
                        await turn_context.send_activity("Draft not found — it may have expired.")
                        return
                    resp.raise_for_status()
                    evaluation = resp.json()

                    draft_resp = await client.get(f"{API_URL}/drafts/{draft_id}")
                    draft_resp.raise_for_status()
                    draft_state = draft_resp.json()

                event = {
                    "passed": evaluation["gate_decision"] == "PASS",
                    "scores": evaluation["scores"],
                    "failure_reasons": evaluation["failure_reasons"],
                    "sharepoint_url": "",
                    "draft_id": draft_id,
                    "rfp_id": evaluation["rfp_id"],
                    "sections": draft_state.get("sections", {}),
                    "edited_section_key": section_key,
                }
                subtitle = f"Edited: {section_key.replace('_', ' ').title()}"
                card = _result_card(event, subtitle)
                await turn_context.send_activity(
                    Activity(type=ActivityTypes.message, attachments=[CardFactory.adaptive_card(card)])
                )
            except Exception as e:
                await turn_context.send_activity(f"Edit failed: {e}")
            return

        if isinstance(value, dict) and value.get("action") == "ai_edit_rfp_section":
            await _apply_ai_edit(
                turn_context,
                value.get("draft_id", ""),
                value.get("section_key", ""),
                value.get("instruction", ""),
            )
            return

        # Check for file attachments — route directly to proposal review.
        # Two Teams attachment formats:
        #   1. Channel file-share: contentType="application/vnd.microsoft.teams.file.download.info"
        #      → pre-authenticated downloadUrl in content dict, no auth header needed
        #   2. Personal chat upload: contentType=MIME type, contentUrl requires Bot Framework bearer token
        _card_types = {"application/vnd.microsoft.card.adaptive", "application/vnd.microsoft.card.thumbnail",
                       "application/vnd.microsoft.card.hero", "application/vnd.microsoft.card.signin"}
        download_url = content_type = att_name = None
        needs_auth = True
        for att in (turn_context.activity.attachments or []):
            if att.content_type == "application/vnd.microsoft.teams.file.download.info":
                # Channel file-share card — pre-authenticated URL in content
                content = att.content if isinstance(att.content, dict) else {}
                if content.get("downloadUrl"):
                    download_url = content["downloadUrl"]
                    att_name = att.name or ""
                    content_type = content.get("fileType", "")
                    needs_auth = False
                    break
            elif att.content_type not in _card_types and att.content_url:
                # Personal chat upload — needs Bot Framework token
                download_url = att.content_url
                att_name = att.name or ""
                content_type = att.content_type or ""
                needs_auth = True
                break

        if download_url:
            await turn_context.send_activity(f"Reading attachment: {att_name}…")
            try:
                proposal_text = await _extract_attachment_text(download_url, content_type, att_name, needs_auth)
                if not proposal_text.strip():
                    await turn_context.send_activity("Could not extract text. Please paste the proposal text directly.")
                    return
                await _handle_review(turn_context, proposal_text)
            except Exception as e:
                await turn_context.send_activity(f"Could not read attachment: {e}\nPlease paste the text directly.")
            return

        text = (turn_context.activity.text or "").strip()
        intent = _detect_intent(text)

        if intent == "classify":
            await _handle_classify(turn_context, text)
            return
        if intent == "review_proposal":
            await _handle_review(turn_context, text)
            return
        if intent == "audit_budget":
            await _handle_budget(turn_context, text)
            return
        if intent == "regulatory_watch":
            await _handle_regulatory(turn_context)
            return

        # No fixed-keyword intent matched — check whether this reads as a typed
        # edit instruction (e.g. "edit the eligibility section to mention CLIA
        # accreditation") before falling through to RFP generation.
        if intent is None:
            section_key = _extract_section_edit(text)
            if section_key:
                await _handle_chat_section_edit(turn_context, section_key, text)
                return

        # generate_rfp or no intent match — try to parse generation params
        params = _parse_rfp_request(text)
        if params is None:
            await turn_context.send_activity(
                "I can help with:\n"
                "- **Draft RFP** — *Draft an influenza RFP, CDC, $2.5M, 24 months*\n"
                "- **Review proposal** — paste proposal text\n"
                "- **Classify** — *Classify: whole genome sequencing surveillance program*\n"
                "- **Budget audit** — paste a budget narrative\n"
                "- **Regulatory watch** — *Any recent CFR changes?*\n"
                "- **Edit a section** — *Edit the eligibility section to mention CLIA accreditation*"
            )
            return

        subtitle = (
            f"{params['program_area'][:40]} · "
            f"${params['total_funding']:,} · "
            f"{params['period_of_performance_months']} months"
        )

        # Send placeholder progress card immediately
        card = _progress_card(completed=[], in_progress=SECTION_ORDER[0], subtitle=subtitle)
        sent = await turn_context.send_activity(
            Activity(type=ActivityTypes.message, attachments=[CardFactory.adaptive_card(card)])
        )
        activity_id = sent.id

        # Stream generation and update card in background
        asyncio.create_task(_stream_and_update(turn_context, activity_id, params, subtitle))


async def _stream_and_update(
    turn_context: TurnContext, activity_id: str, params: dict, subtitle: str
) -> None:
    completed: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            async with client.stream("POST", f"{API_URL}/generate/stream", json=params) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    event = json.loads(line)

                    if event["type"] == "section":
                        section_key = event["section_key"]
                        completed.append(section_key)
                        idx = completed.index(section_key)
                        next_key = SECTION_ORDER[idx + 1] if idx + 1 < len(SECTION_ORDER) else None
                        card = _progress_card(completed=completed, in_progress=next_key, subtitle=subtitle)
                        await turn_context.update_activity(
                            Activity(id=activity_id, type=ActivityTypes.message,
                                     attachments=[CardFactory.adaptive_card(card)])
                        )

                    elif event["type"] == "gate_result":
                        card = _result_card(event, subtitle)
                        await turn_context.update_activity(
                            Activity(id=activity_id, type=ActivityTypes.message,
                                     attachments=[CardFactory.adaptive_card(card)])
                        )

                    elif event["type"] == "error":
                        await turn_context.update_activity(
                            Activity(id=activity_id, type=ActivityTypes.message,
                                     text=f"Generation error: {event.get('message', 'unknown error')}. Please try again.")
                        )

    except Exception:
        log.exception("Streaming generation failed for activity %s", activity_id)
        await turn_context.update_activity(
            Activity(id=activity_id, type=ActivityTypes.message,
                     text="Generation failed. Please try again or contact your administrator.")
        )


# ── Parameter parsing ───────────────────────────────────────────────────────────

def _parse_rfp_request(text: str) -> dict | None:
    """Extract RFP parameters from natural-language input.
    Returns None if the message doesn't look like a generation request.
    In production this is replaced by the /classify agent call.
    """
    keywords = ["rfp", "draft", "request for proposal", "grant", "funding", "cooperative agreement"]
    if not any(kw in text.lower() for kw in keywords):
        return None

    amount_match = re.search(r"\$?([\d.]+)\s*([mk])\b", text, re.IGNORECASE)
    amount = 0
    if amount_match:
        val = float(amount_match.group(1))
        unit = amount_match.group(2).lower()
        amount = int(val * (1_000_000 if unit == "m" else 1_000))

    months_match = re.search(r"(\d+)[\s-]month", text, re.IGNORECASE)
    months = int(months_match.group(1)) if months_match else 24

    sponsor = "CDC"
    for s in ["HRSA", "HHS", "NIH", "FEMA", "EPA"]:
        if s.lower() in text.lower():
            sponsor = s
            break

    return {
        "program_area": text[:200],
        "federal_sponsor": sponsor,
        "total_funding": amount or 1_000_000,
        "period_of_performance_months": months,
        "estimated_awards_min": 1,
        "estimated_awards_max": 5,
        "award_range_min": 100_000,
        "award_range_max": amount or 1_000_000,
        "cost_sharing_required": "cost shar" in text.lower(),
        "key_requirements": [],
        "write_to_sharepoint": False,
        "write_to_fabric": True,
    }
