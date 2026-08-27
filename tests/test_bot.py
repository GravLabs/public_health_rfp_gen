"""Unit tests for the Teams bot (src/bot/bot.py).

Covers: intent detection, natural-language param parsing, all prompt handlers
(generate/review/classify/budget/regulatory), the Adaptive Card
"Approve & Save to SharePoint" button, the help fallback, and the file-attachment
upload/review scenario (Teams channel file-share + personal-chat upload, PDF/DOCX/
plain text). All Bot Framework and HTTP calls are mocked or faked — no real Teams,
Azure, or network access is used, so this can run after every deploy/Teams-app
upload without touching live infra.
"""
from __future__ import annotations

import io
import json
import sys
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "bot"))

import bot  # noqa: E402
from botbuilder.core import ActivityHandler  # noqa: E402  (sanity: real SDK types available)


# ── Fakes ────────────────────────────────────────────────────────────────────────

class FakeTurnContext:
    """Duck-typed stand-in for botbuilder's TurnContext.

    Only implements what RfpBotHandler actually touches: `.activity` and the two
    async send/update methods. Captures everything sent so tests can assert on it
    without spinning up a real Bot Framework adapter.
    """

    def __init__(self, text: str = "", value: dict | None = None, attachments: list | None = None):
        self.activity = SimpleNamespace(text=text, value=value, attachments=attachments or [])
        self.sent: list = []
        self.updated: list = []
        self._next_id = 0

    async def send_activity(self, activity):
        self._next_id += 1
        self.sent.append(activity)
        return SimpleNamespace(id=f"activity-{self._next_id}")

    async def update_activity(self, activity):
        self.updated.append(activity)


def _text_of(activity) -> str:
    """Extract displayable text from either a plain string or an Activity."""
    if isinstance(activity, str):
        return activity
    return getattr(activity, "text", "") or ""


def _card_of(activity) -> dict:
    """Extract the Adaptive Card dict from an Activity's first attachment."""
    return activity.attachments[0].content


def _mock_response(json_data=None, content=None, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    if status_code >= 400:
        import httpx
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=resp
        )
    else:
        resp.raise_for_status = MagicMock()
    if json_data is not None:
        resp.json.return_value = json_data
    if content is not None:
        resp.content = content
    return resp


class _FakeStream:
    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FailingStream:
    async def __aenter__(self):
        raise RuntimeError("connection reset")

    async def __aexit__(self, *a):
        return False


class RoutedAsyncClient:
    """Fake httpx.AsyncClient that dispatches GET/POST/stream by URL substring.

    `routes` maps (METHOD, url_substring) -> response (or a callable(**kwargs) -> response).
    `calls`, if provided, gets every (method, url, kwargs) appended for assertions
    (e.g. verifying an Authorization header was set).
    """

    def __init__(self, routes: dict, calls: list | None = None, **_kwargs):
        self._routes = routes
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def _resolve(self, method, url, **kwargs):
        if self._calls is not None:
            self._calls.append((method, url, kwargs))
        for (m, sub), resp in self._routes.items():
            if m == method and sub in url:
                # MagicMock responses are themselves callable, so only treat plain
                # functions/lambdas as dynamic handlers — static mocks pass through.
                if callable(resp) and not isinstance(resp, MagicMock):
                    return resp(**kwargs)
                return resp
        raise AssertionError(f"Unmocked {method} {url}")

    async def get(self, url, **kwargs):
        return self._resolve("GET", url, **kwargs)

    async def post(self, url, **kwargs):
        return self._resolve("POST", url, **kwargs)

    def stream(self, method, url, **kwargs):
        return self._resolve("STREAM", url, **kwargs)


def _client_factory(routes: dict, calls: list | None = None):
    def _factory(*args, **kwargs):
        return RoutedAsyncClient(routes, calls=calls)

    return _factory


# ── Section-edit instruction detection ────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("Edit the eligibility section to mention CLIA accreditation", "eligibility"),
    ("update the funding parameters to require quarterly reports", "funding_parameters"),
    ("revise scope of work to add a timeline", "scope_of_work"),
    ("rewrite background", "background"),
    ("audit this budget for allowable costs", None),  # no edit verb
    ("give me an update on recent CFR changes", None),  # no section reference
    ("What's the weather today?", None),
])
def test_extract_section_edit(text, expected):
    assert bot._extract_section_edit(text) == expected


def test_detect_intent_takes_priority_over_section_edit_extraction():
    # "budget" would also satisfy _extract_section_edit's section-reference check
    # (budget_requirements), and "update" is an edit verb -- but _detect_intent's
    # audit_budget match must win so this doesn't get misrouted into an AI edit.
    text = "please update this budget for allowable costs"
    assert bot._detect_intent(text) == "audit_budget"


# ── Intent detection ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("Draft an influenza RFP, CDC, $2.5M, 24 months", "generate_rfp"),
    ("Please review this proposal from Lab X", "review_proposal"),
    ("Classify: whole genome sequencing surveillance program", "classify"),
    ("Any recent CFR changes?", "regulatory_watch"),
    ("Check this budget line item for allowable costs", "audit_budget"),
    ("hello there", None),
    ("What's the weather today?", None),
])
def test_detect_intent(text, expected):
    assert bot._detect_intent(text) == expected


# ── Natural-language parameter parsing ────────────────────────────────────────────

def test_parse_rfp_request_full():
    params = bot._parse_rfp_request("Draft an influenza RFP, CDC, $2.5M, 24 months")
    assert params is not None
    assert params["total_funding"] == 2_500_000
    assert params["period_of_performance_months"] == 24
    assert params["federal_sponsor"] == "CDC"
    assert params["write_to_sharepoint"] is False


def test_parse_rfp_request_sponsor_detection():
    params = bot._parse_rfp_request("Draft an RFP for HRSA workforce program, $500k, 12 months")
    assert params["federal_sponsor"] == "HRSA"
    assert params["total_funding"] == 500_000
    assert params["period_of_performance_months"] == 12


def test_parse_rfp_request_cost_sharing_detected():
    params = bot._parse_rfp_request("Draft an RFP with cost sharing required, $1M")
    assert params["cost_sharing_required"] is True


def test_parse_rfp_request_defaults_when_unspecified():
    params = bot._parse_rfp_request("Draft an RFP for a new program")
    assert params["federal_sponsor"] == "CDC"
    assert params["total_funding"] == 1_000_000
    assert params["period_of_performance_months"] == 24


def test_parse_rfp_request_returns_none_for_non_rfp_text():
    assert bot._parse_rfp_request("What's the weather today?") is None


# ── Card builders ─────────────────────────────────────────────────────────────────

def test_progress_card_marks_completed_and_in_progress():
    card = bot._progress_card(completed=["background", "funding_parameters"],
                               in_progress="eligibility", subtitle="Test RFP")
    text_by_section = {}
    for row in card["body"][-1]["items"]:
        label = row["columns"][1]["items"][0]["text"]
        icon = row["columns"][0]["items"][0]["text"]
        text_by_section[label] = icon
    assert text_by_section["Background and Purpose"] == "✓"
    assert text_by_section["Eligibility Criteria"] == "⏳"
    assert text_by_section["Scope of Work"] == "○"


def test_result_card_always_offers_draft_preview_link_first():
    event = {"passed": False, "sharepoint_url": "", "draft_id": "d1",
              "scores": {}, "failure_reasons": [], "rfp_id": "RFP-1"}
    card = bot._result_card(event, "subtitle")
    assert card["actions"][0]["type"] == "Action.OpenUrl"
    assert card["actions"][0]["url"] == bot._draft_preview_url("d1")


def test_draft_preview_url_is_a_teams_entity_deep_link_with_fallback():
    url = bot._draft_preview_url("d1")
    assert url.startswith(f"https://teams.microsoft.com/l/entity/{bot.APP_ID}/{bot.DRAFT_PREVIEW_ENTITY_ID}")
    assert urllib.parse.quote(f"{bot.PUBLIC_API_URL}/drafts/d1/view", safe="") in url


def test_result_card_passed_with_sharepoint_url_offers_open_link():
    event = {"passed": True, "sharepoint_url": "https://sp/doc.docx", "draft_id": "d1",
              "scores": {}, "failure_reasons": [], "rfp_id": "RFP-1"}
    card = bot._result_card(event, "subtitle")
    assert card["actions"][1]["type"] == "Action.OpenUrl"
    assert card["actions"][1]["url"] == "https://sp/doc.docx"


def test_result_card_passed_without_url_offers_approve_button():
    event = {"passed": True, "sharepoint_url": "", "draft_id": "d1",
              "scores": {}, "failure_reasons": [], "rfp_id": "RFP-1"}
    card = bot._result_card(event, "subtitle")
    assert card["actions"][1]["type"] == "Action.Submit"
    assert card["actions"][1]["data"] == {"action": "approve_rfp", "draft_id": "d1"}


def test_result_card_failed_offers_reject_but_no_approve_and_lists_reasons():
    event = {"passed": False, "sharepoint_url": "", "draft_id": "d1",
              "scores": {"coherence": 0.5}, "failure_reasons": ["coherence below threshold"],
              "rfp_id": "RFP-1"}
    card = bot._result_card(event, "subtitle")
    # Reject/Edit are offered regardless of gate pass/fail; Approve is not (no sections to edit here).
    submit_actions = [a["data"]["action"] for a in card["actions"] if a["type"] == "Action.Submit"]
    assert submit_actions == ["reject_rfp"]
    assert any("coherence below threshold" in b.get("text", "") for b in card["body"])


def test_result_card_with_sections_offers_edit_action():
    event = {"passed": False, "sharepoint_url": "", "draft_id": "d1",
              "scores": {}, "failure_reasons": [], "rfp_id": "RFP-1",
              "sections": {"background": "Some background text."}}
    card = bot._result_card(event, "subtitle")
    assert card["actions"][-1]["type"] == "Action.ShowCard"
    assert card["actions"][-1]["card"]["actions"][0]["data"] == {"action": "edit_rfp_section", "draft_id": "d1"}


def test_result_card_shows_edited_section_label():
    event = {"passed": True, "sharepoint_url": "", "draft_id": "d1",
              "scores": {}, "failure_reasons": [], "rfp_id": "RFP-1",
              "edited_section_key": "eligibility"}
    card = bot._result_card(event, "Edited: Eligibility")
    assert any(b.get("text") == "Updated: Eligibility" for b in card["body"])


# ── Prompt handlers (via on_message_activity) ─────────────────────────────────────

@pytest.mark.asyncio
async def test_on_message_classify():
    routes = {("POST", "/classify"): _mock_response(json_data={
        "program_area": "Genomic Surveillance", "confidence": 0.92, "rationale": "Mentions WGS.",
    })}
    with patch("bot.httpx.AsyncClient", side_effect=_client_factory(routes)):
        ctx = FakeTurnContext(text="Classify: whole genome sequencing surveillance program")
        await bot.RfpBotHandler().on_message_activity(ctx)

    assert _text_of(ctx.sent[0]) == "Classifying program area…"
    card = _card_of(ctx.sent[1])
    assert card["body"][2]["text"] == "Genomic Surveillance"


@pytest.mark.asyncio
async def test_on_message_review_proposal():
    routes = {("POST", "/review"): _mock_response(json_data={
        "recommendation": "fund", "total_score": 88, "scores": {}, "flags": [],
    })}
    with patch("bot.httpx.AsyncClient", side_effect=_client_factory(routes)):
        ctx = FakeTurnContext(text="Please review this proposal from Lab X for funding")
        await bot.RfpBotHandler().on_message_activity(ctx)

    assert "Reviewing proposal" in _text_of(ctx.sent[0])
    card = _card_of(ctx.sent[1])
    assert card["body"][0]["text"] == "Proposal Review"


@pytest.mark.asyncio
async def test_on_message_audit_budget_parses_dollar_amount():
    captured = {}

    def _post(**kwargs):
        captured.update(kwargs.get("json", {}))
        return _mock_response(json_data={"recommendation": "approve", "total_verified": True,
                                          "line_items": [], "flags": []})

    routes = {("POST", "/audit_budget"): _post}
    with patch("bot.httpx.AsyncClient", side_effect=_client_factory(routes)):
        ctx = FakeTurnContext(text="Check this $2,500,000 budget for allowable costs")
        await bot.RfpBotHandler().on_message_activity(ctx)

    assert captured["total_funding"] == 2_500_000
    card = _card_of(ctx.sent[1])
    assert card["body"][0]["text"] == "Budget Audit"


@pytest.mark.asyncio
async def test_on_message_regulatory_watch_success():
    routes = {("POST", "/regulatory_watch"): _mock_response(json_data={
        "alerts": [], "checked_at": "2026-08-19T00:00:00",
    })}
    with patch("bot.httpx.AsyncClient", side_effect=_client_factory(routes)):
        ctx = FakeTurnContext(text="Any recent CFR changes?")
        await bot.RfpBotHandler().on_message_activity(ctx)

    card = _card_of(ctx.sent[1])
    assert card["body"][0]["text"] == "Regulatory Watch"


@pytest.mark.asyncio
async def test_on_message_regulatory_watch_api_error_reported_to_user():
    routes = {("POST", "/regulatory_watch"): _mock_response(status_code=500)}
    with patch("bot.httpx.AsyncClient", side_effect=_client_factory(routes)):
        ctx = FakeTurnContext(text="Any recent CFR changes?")
        await bot.RfpBotHandler().on_message_activity(ctx)

    assert "Regulatory watch error" in _text_of(ctx.sent[-1])


@pytest.mark.asyncio
async def test_on_message_unrecognized_text_shows_help():
    ctx = FakeTurnContext(text="hello there")
    await bot.RfpBotHandler().on_message_activity(ctx)

    assert "I can help with" in _text_of(ctx.sent[0])
    assert "Draft RFP" in _text_of(ctx.sent[0])


# ── Approve & Save to SharePoint button ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_approve_rfp_action_success():
    routes = {("POST", "/export/d1"): _mock_response(json_data={
        "sharepoint_url": "https://sp/rfp.docx", "rfp_id": "RFP-1",
    })}
    with patch("bot.httpx.AsyncClient", side_effect=_client_factory(routes)):
        ctx = FakeTurnContext(value={"action": "approve_rfp", "draft_id": "d1"})
        await bot.RfpBotHandler().on_message_activity(ctx)

    final = _text_of(ctx.sent[-1])
    assert "Saved to SharePoint" in final
    assert "https://sp/rfp.docx" in final


@pytest.mark.asyncio
async def test_approve_rfp_action_draft_expired():
    routes = {("POST", "/export/gone"): _mock_response(status_code=404)}
    with patch("bot.httpx.AsyncClient", side_effect=_client_factory(routes)):
        ctx = FakeTurnContext(value={"action": "approve_rfp", "draft_id": "gone"})
        await bot.RfpBotHandler().on_message_activity(ctx)

    assert "may have expired" in _text_of(ctx.sent[-1])


@pytest.mark.asyncio
async def test_approve_rfp_action_export_failure_reported():
    routes = {("POST", "/export/d1"): _mock_response(status_code=500)}
    with patch("bot.httpx.AsyncClient", side_effect=_client_factory(routes)):
        ctx = FakeTurnContext(value={"action": "approve_rfp", "draft_id": "d1"})
        await bot.RfpBotHandler().on_message_activity(ctx)

    assert "SharePoint upload failed" in _text_of(ctx.sent[-1])


# ── AI Edit (typed chat instruction — no card action; removed per feedback) ───────

def _ai_edit_routes(draft_id="d1"):
    return {
        ("POST", f"/drafts/{draft_id}/edit/ai"): _mock_response(json_data={
            "gate_decision": "PASS", "rfp_id": "RFP-1",
            "scores": {"completeness": 1.0, "parameter_accuracy": 1.0, "compliance": 0.9,
                       "groundedness": 0.9, "coherence": 0.9},
            "failure_reasons": [],
        }),
        ("GET", f"/drafts/{draft_id}"): _mock_response(json_data={
            "draft_id": draft_id, "rfp_id": "RFP-1", "status": "PENDING",
            "sections": {"eligibility": "Only CLIA-accredited labs may apply."},
        }),
    }


@pytest.mark.asyncio
async def test_apply_ai_edit_success():
    with patch("bot.httpx.AsyncClient", side_effect=_client_factory(_ai_edit_routes())):
        ctx = FakeTurnContext()
        await bot._apply_ai_edit(ctx, "d1", "eligibility", "mention CLIA accreditation")

    assert "Rewriting section" in _text_of(ctx.sent[0])
    card = _card_of(ctx.sent[-1])
    assert card["body"][0]["text"] == "✓ Draft Ready — Gate Passed"
    assert any(b.get("text") == "AI-edited: Eligibility" for b in card["body"])


@pytest.mark.asyncio
async def test_apply_ai_edit_draft_expired():
    routes = {("POST", "/drafts/gone/edit/ai"): _mock_response(status_code=404)}
    with patch("bot.httpx.AsyncClient", side_effect=_client_factory(routes)):
        ctx = FakeTurnContext()
        await bot._apply_ai_edit(ctx, "gone", "eligibility", "shorten it")

    assert "may have expired" in _text_of(ctx.sent[-1])


@pytest.mark.asyncio
async def test_apply_ai_edit_unknown_section_reports_detail():
    routes = {("POST", "/drafts/d1/edit/ai"): _mock_response(
        status_code=400, json_data={"detail": "Unknown section: not_a_real_section"},
    )}
    with patch("bot.httpx.AsyncClient", side_effect=_client_factory(routes)):
        ctx = FakeTurnContext()
        await bot._apply_ai_edit(ctx, "d1", "not_a_real_section", "do something")

    assert "Unknown section: not_a_real_section" in _text_of(ctx.sent[-1])


@pytest.mark.asyncio
async def test_typed_section_edit_resolves_latest_draft():
    routes = {("GET", "/drafts/latest"): _mock_response(json_data={"draft_id": "d1"}),
              **_ai_edit_routes()}
    with patch("bot.httpx.AsyncClient", side_effect=_client_factory(routes)):
        ctx = FakeTurnContext(text="Edit the eligibility section to mention CLIA accreditation")
        await bot.RfpBotHandler().on_message_activity(ctx)

    card = _card_of(ctx.sent[-1])
    assert any(b.get("text") == "AI-edited: Eligibility" for b in card["body"])


@pytest.mark.asyncio
async def test_typed_section_edit_no_draft_yet():
    routes = {("GET", "/drafts/latest"): _mock_response(status_code=404)}
    with patch("bot.httpx.AsyncClient", side_effect=_client_factory(routes)):
        ctx = FakeTurnContext(text="Edit the eligibility section to mention CLIA accreditation")
        await bot.RfpBotHandler().on_message_activity(ctx)

    assert "No draft to edit yet" in _text_of(ctx.sent[-1])


# ── Upload scenario: Teams file attachments routed to proposal review ─────────────

def _make_docx_bytes(paragraphs: list[str]) -> bytes:
    from docx import Document as DocxDocument
    doc = DocxDocument()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_upload_channel_file_share_pdf_routes_to_review():
    fake_reader = MagicMock()
    fake_page = MagicMock()
    fake_page.extract_text.return_value = "Proposal narrative from PDF."
    fake_reader.return_value.pages = [fake_page]

    routes = {
        ("GET", "https://sp.contoso/files/proposal.pdf"): _mock_response(content=b"%PDF-1.4 fake"),
        ("POST", "/review"): _mock_response(json_data={
            "recommendation": "revise", "total_score": 60, "scores": {}, "flags": [],
        }),
    }
    att = SimpleNamespace(
        content_type="application/vnd.microsoft.teams.file.download.info",
        content={"downloadUrl": "https://sp.contoso/files/proposal.pdf", "fileType": "application/pdf"},
        name="proposal.pdf",
        content_url=None,
    )
    with patch("bot.httpx.AsyncClient", side_effect=_client_factory(routes)), \
         patch("pypdf.PdfReader", fake_reader):
        ctx = FakeTurnContext(attachments=[att])
        await bot.RfpBotHandler().on_message_activity(ctx)

    assert "Reading attachment: proposal.pdf" in _text_of(ctx.sent[0])
    assert "Reviewing proposal" in _text_of(ctx.sent[1])
    card = _card_of(ctx.sent[2])
    assert card["body"][0]["text"] == "Proposal Review"


@pytest.mark.asyncio
async def test_upload_personal_chat_docx_requires_bot_token_and_routes_to_review():
    docx_bytes = _make_docx_bytes(["A real proposal paragraph.", "Second paragraph."])
    calls: list = []
    routes = {
        ("POST", "login.microsoftonline.com"): _mock_response(json_data={"access_token": "fake-token"}),
        ("GET", "https://teams.contoso/attachments/1"): _mock_response(content=docx_bytes),
        ("POST", "/review"): _mock_response(json_data={
            "recommendation": "fund", "total_score": 91, "scores": {}, "flags": [],
        }),
    }
    att = SimpleNamespace(
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        content=None,
        name="proposal.docx",
        content_url="https://teams.contoso/attachments/1",
    )
    with patch("bot.httpx.AsyncClient", side_effect=_client_factory(routes, calls=calls)):
        ctx = FakeTurnContext(attachments=[att])
        await bot.RfpBotHandler().on_message_activity(ctx)

    # Personal-chat uploads require a Bot Framework bearer token
    get_calls = [c for c in calls if c[0] == "GET"]
    assert get_calls, "expected a GET for the attachment content"
    assert get_calls[0][2]["headers"]["Authorization"] == "Bearer fake-token"

    card = _card_of(ctx.sent[-1])
    assert card["body"][0]["text"] == "Proposal Review"


@pytest.mark.asyncio
async def test_upload_plain_text_attachment_decodes_directly():
    routes = {
        ("GET", "https://sp.contoso/files/notes.txt"): _mock_response(content=b"Plain proposal notes."),
        ("POST", "/review"): _mock_response(json_data={
            "recommendation": "revise", "total_score": 50, "scores": {}, "flags": [],
        }),
    }
    att = SimpleNamespace(
        content_type="application/vnd.microsoft.teams.file.download.info",
        content={"downloadUrl": "https://sp.contoso/files/notes.txt", "fileType": "text/plain"},
        name="notes.txt",
        content_url=None,
    )
    with patch("bot.httpx.AsyncClient", side_effect=_client_factory(routes)):
        ctx = FakeTurnContext(attachments=[att])
        await bot.RfpBotHandler().on_message_activity(ctx)

    assert "Proposal Review" in _card_of(ctx.sent[-1])["body"][0]["text"]


@pytest.mark.asyncio
async def test_upload_empty_extracted_text_prompts_for_paste():
    routes = {
        ("GET", "https://sp.contoso/files/empty.txt"): _mock_response(content=b"   \n  "),
    }
    att = SimpleNamespace(
        content_type="application/vnd.microsoft.teams.file.download.info",
        content={"downloadUrl": "https://sp.contoso/files/empty.txt", "fileType": "text/plain"},
        name="empty.txt",
        content_url=None,
    )
    with patch("bot.httpx.AsyncClient", side_effect=_client_factory(routes)):
        ctx = FakeTurnContext(attachments=[att])
        await bot.RfpBotHandler().on_message_activity(ctx)

    assert "Could not extract text" in _text_of(ctx.sent[-1])


@pytest.mark.asyncio
async def test_upload_download_failure_reported_to_user():
    routes = {
        ("GET", "https://sp.contoso/files/broken.pdf"): _mock_response(status_code=500),
    }
    att = SimpleNamespace(
        content_type="application/vnd.microsoft.teams.file.download.info",
        content={"downloadUrl": "https://sp.contoso/files/broken.pdf", "fileType": "application/pdf"},
        name="broken.pdf",
        content_url=None,
    )
    with patch("bot.httpx.AsyncClient", side_effect=_client_factory(routes)):
        ctx = FakeTurnContext(attachments=[att])
        await bot.RfpBotHandler().on_message_activity(ctx)

    assert "Could not read attachment" in _text_of(ctx.sent[-1])


# ── Full "generate RFP" prompt: streaming generation + card updates ──────────────

@pytest.mark.asyncio
async def test_generate_rfp_prompt_streams_progress_and_final_card(monkeypatch):
    import asyncio

    lines = [
        json.dumps({"type": "section", "section_key": "background"}),
        json.dumps({"type": "section", "section_key": "funding_parameters"}),
        json.dumps({
            "type": "gate_result", "passed": True, "draft_id": "d1", "rfp_id": "RFP-1",
            "scores": {"completeness": 1.0, "parameter_accuracy": 1.0, "compliance": 0.8,
                       "groundedness": 1.0, "coherence": 0.9},
            "failure_reasons": [], "sharepoint_url": "",
            "sections": {"background": "Generated background text."},
        }),
    ]
    routes = {("STREAM", "/generate/stream"): _FakeStream(lines)}

    created: dict = {}

    def fake_create_task(coro):
        created["task"] = asyncio.ensure_future(coro)
        return created["task"]

    monkeypatch.setattr(bot.asyncio, "create_task", fake_create_task)

    with patch("bot.httpx.AsyncClient", side_effect=_client_factory(routes)):
        ctx = FakeTurnContext(text="Draft an influenza RFP, CDC, $2.5M, 24 months")
        await bot.RfpBotHandler().on_message_activity(ctx)
        await created["task"]

    # Initial progress card + one update per section + one update for the result card.
    # No follow-up dump of the full draft text — Draft Preview (the pinned Teams tab /
    # Open Draft Preview button) is where a human reads the actual content now.
    assert _card_of(ctx.sent[0])["body"][0]["text"] == "Generating RFP Draft"
    assert len(ctx.updated) == 3
    assert ctx.updated[-1].attachments[0].content["body"][0]["text"] == "✓ Draft Ready — Gate Passed"
    assert len(ctx.sent) == 1


@pytest.mark.asyncio
async def test_generate_rfp_prompt_handles_error_event(monkeypatch):
    import asyncio

    lines = [json.dumps({"type": "error", "message": "orchestrator timeout"})]
    routes = {("STREAM", "/generate/stream"): _FakeStream(lines)}

    created: dict = {}

    def fake_create_task(coro):
        created["task"] = asyncio.ensure_future(coro)
        return created["task"]

    monkeypatch.setattr(bot.asyncio, "create_task", fake_create_task)

    with patch("bot.httpx.AsyncClient", side_effect=_client_factory(routes)):
        ctx = FakeTurnContext(text="Draft an influenza RFP, CDC, $2.5M, 24 months")
        await bot.RfpBotHandler().on_message_activity(ctx)
        await created["task"]

    assert "Generation error: orchestrator timeout" in ctx.updated[-1].text


@pytest.mark.asyncio
async def test_generate_rfp_prompt_handles_stream_connection_failure(monkeypatch):
    import asyncio

    routes = {("STREAM", "/generate/stream"): _FailingStream()}

    created: dict = {}

    def fake_create_task(coro):
        created["task"] = asyncio.ensure_future(coro)
        return created["task"]

    monkeypatch.setattr(bot.asyncio, "create_task", fake_create_task)

    with patch("bot.httpx.AsyncClient", side_effect=_client_factory(routes)):
        ctx = FakeTurnContext(text="Draft an influenza RFP, CDC, $2.5M, 24 months")
        await bot.RfpBotHandler().on_message_activity(ctx)
        await created["task"]

    assert "Generation failed" in ctx.updated[-1].text
