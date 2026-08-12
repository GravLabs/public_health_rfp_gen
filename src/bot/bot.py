"""
Teams Bot — RFP Generation ActivityHandler.

Receives messages via Azure Bot Service webhook (/api/messages).
For generation requests, sends an initial Adaptive Card progress
display and updates it in-place as each of the 8 RFP sections
completes, then shows the final gate result with a SharePoint link.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

import httpx
from botbuilder.core import ActivityHandler, CardFactory, TurnContext
from botbuilder.schema import Activity, ActivityTypes

log = logging.getLogger(__name__)

API_URL = os.getenv("API_URL", "http://localhost:8000")

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

    actions = []
    if passed and sp_url:
        actions.append({
            "type": "Action.OpenUrl",
            "title": "Open Word Draft in SharePoint",
            "url": sp_url,
        })

    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.5",
        "body": body,
        "actions": actions,
    }


# ── Bot handler ─────────────────────────────────────────────────────────────────

class RfpBotHandler(ActivityHandler):
    async def on_message_activity(self, turn_context: TurnContext):
        text = (turn_context.activity.text or "").strip()
        params = _parse_rfp_request(text)

        if params is None:
            await turn_context.send_activity(
                "Describe the RFP you need — include the program area, funding amount, and performance period. "
                "Example: *Draft an influenza surveillance RFP, CDC funding, $2.5M total, 24 months.*"
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
                        if event.get("passed") and event.get("sections"):
                            preview = _format_rfp_markdown(
                                event.get("rfp_id", ""), event["sections"]
                            )
                            await turn_context.send_activity(
                                Activity(type=ActivityTypes.message, text=preview)
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


# ── Markdown preview ────────────────────────────────────────────────────────────

SECTION_HEADINGS = {
    "background":             "1. Background and Purpose",
    "funding_parameters":     "2. Funding Parameters",
    "eligibility":            "3. Eligibility Criteria",
    "scope_of_work":          "4. Scope of Work",
    "reporting_requirements": "5. Reporting Requirements",
    "budget_requirements":    "6. Budget Requirements",
    "evaluation_criteria":    "7. Evaluation Criteria",
    "submission_instructions":"8. Submission Instructions",
}


def _format_rfp_markdown(rfp_id: str, sections: dict[str, str]) -> str:
    """Render all 8 RFP sections as a markdown Teams message."""
    lines = [f"📄 **RFP Draft — {rfp_id}**\n*AI-generated · pending human review*\n"]
    for key, heading in SECTION_HEADINGS.items():
        content = sections.get(key, "").strip()
        if content:
            lines.append(f"\n---\n## {heading}\n\n{content}")
    return "\n".join(lines)


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
        "write_to_sharepoint": True,
        "write_to_fabric": True,
    }
