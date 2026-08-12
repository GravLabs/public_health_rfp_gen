"""Unit tests for SharePointClient — all network calls are mocked."""
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "api"))
from sharepoint_client import SharePointClient


FAKE_SITE_ID = "contoso.sharepoint.com,abc-123,def-456"


@pytest.fixture
def client():
    with patch("sharepoint_client.DefaultAzureCredential"), \
         patch("sharepoint_client.get_bearer_token_provider", return_value=lambda: "fake-token"):
        return SharePointClient(FAKE_SITE_ID)


@pytest.mark.asyncio
async def test_list_library_files_returns_files(client):
    drives_response = {"value": [{"id": "drive-1", "name": "Documents"}]}
    items_response = {
        "value": [
            {"id": "item-1", "name": "RFP-001.pdf", "size": 1024, "webUrl": "https://sp/rfp-001.pdf",
             "file": {"mimeType": "application/pdf"}},
            {"id": "item-2", "name": "README.txt", "size": 100,
             "file": {"mimeType": "text/plain"}},
            {"id": "folder-1", "name": "Archive"},  # folder — no "file" key, should be excluded
        ]
    }

    mock_resp_drives = MagicMock()
    mock_resp_drives.raise_for_status = MagicMock()
    mock_resp_drives.json.return_value = drives_response

    mock_resp_items = MagicMock()
    mock_resp_items.raise_for_status = MagicMock()
    mock_resp_items.json.return_value = items_response

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=[mock_resp_drives, mock_resp_items])

    with patch("sharepoint_client.httpx.AsyncClient", return_value=mock_client):
        files = await client.list_library_files("Documents")

    assert len(files) == 2  # folder excluded
    assert files[0]["name"] == "RFP-001.pdf"
    assert files[0]["drive_id"] == "drive-1"


@pytest.mark.asyncio
async def test_list_library_raises_when_library_not_found(client):
    drives_response = {"value": [{"id": "drive-1", "name": "Documents"}]}
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = drives_response

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("sharepoint_client.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(ValueError, match="Library 'NonExistent' not found"):
            await client.list_library_files("NonExistent")


@pytest.mark.asyncio
async def test_download_file_returns_bytes(client):
    fake_content = b"%PDF-1.4 fake content"
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.content = fake_content

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("sharepoint_client.httpx.AsyncClient", return_value=mock_client):
        result = await client.download_file("drive-1", "item-1")

    assert result == fake_content


@pytest.mark.asyncio
async def test_upload_draft_returns_url(client):
    drives_response = {"value": [{"id": "drive-1", "name": "Generated Drafts"}]}
    upload_response = {"webUrl": "https://sp/GeneratedDrafts/test.md"}

    mock_drives_resp = MagicMock()
    mock_drives_resp.raise_for_status = MagicMock()
    mock_drives_resp.json.return_value = drives_response

    mock_upload_resp = MagicMock()
    mock_upload_resp.raise_for_status = MagicMock()
    mock_upload_resp.json.return_value = upload_response

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_drives_resp)
    mock_client.put = AsyncMock(return_value=mock_upload_resp)

    with patch("sharepoint_client.httpx.AsyncClient", return_value=mock_client):
        url = await client.upload_draft("Generated Drafts", "test.md", "# Test RFP")

    assert url == "https://sp/GeneratedDrafts/test.md"


def test_draft_to_markdown_all_sections():
    sections = {
        "background": "Background text here.",
        "funding_parameters": "| Total | $1M |",
        "eligibility": "- CLIA required",
        "scope_of_work": "A. Do things",
        "reporting_requirements": "Quarterly reports",
        "budget_requirements": "Reagents allowable",
        "evaluation_criteria": "| Tech | 30 |",
        "submission_instructions": "Submit by Dec 1",
    }
    md = SharePointClient.draft_to_markdown("Public Health Labs-RFP-2024-TEST-001", sections)
    assert "Public Health Labs-RFP-2024-TEST-001" in md
    assert "Background and Purpose" in md
    assert "Scope of Work" in md
    assert "Background text here." in md
    assert "AI-generated draft" in md


def test_draft_to_markdown_handles_missing_sections():
    sections = {"background": "Only background present."}
    md = SharePointClient.draft_to_markdown("TEST-001", sections)
    assert "Only background present." in md
    assert "_Not generated_" in md
