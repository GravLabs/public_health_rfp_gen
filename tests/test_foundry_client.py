"""Unit tests for ContentSafetyClient (src/api/foundry_client.py)."""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "api"))


@pytest.mark.asyncio
async def test_content_safety_returns_safe_for_clean_text():
    safe_response = {"categoriesAnalysis": [
        {"category": "Hate", "severity": 0},
        {"category": "SelfHarm", "severity": 0},
        {"category": "Violence", "severity": 0},
    ]}
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = safe_response

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("foundry_client.DefaultAzureCredential"), \
         patch("foundry_client.CONTENT_SAFETY_ENDPOINT", "https://fake.cognitiveservices.azure.com"), \
         patch("foundry_client.get_bearer_token_provider", return_value=lambda: "tok"), \
         patch("foundry_client.httpx.AsyncClient", return_value=mock_client):
        from foundry_client import ContentSafetyClient
        client = ContentSafetyClient()
        client._endpoint = "https://fake.cognitiveservices.azure.com"
        is_safe, flagged = await client.is_safe("This is a compliant public health RFP.")

    assert is_safe is True
    assert flagged == []


@pytest.mark.asyncio
async def test_content_safety_flags_harmful_content():
    flagged_response = {"categoriesAnalysis": [
        {"category": "Hate", "severity": 4},
        {"category": "Violence", "severity": 0},
    ]}
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = flagged_response

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("foundry_client.DefaultAzureCredential"), \
         patch("foundry_client.get_bearer_token_provider", return_value=lambda: "tok"), \
         patch("foundry_client.httpx.AsyncClient", return_value=mock_client):
        from foundry_client import ContentSafetyClient
        client = ContentSafetyClient()
        client._endpoint = "https://fake.cognitiveservices.azure.com"
        is_safe, flagged = await client.is_safe("Harmful content here.")

    assert is_safe is False
    assert "Hate" in flagged


@pytest.mark.asyncio
async def test_content_safety_allows_through_on_api_failure():
    """Safety check must not block generation when the service is down."""
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(side_effect=Exception("timeout"))

    with patch("foundry_client.DefaultAzureCredential"), \
         patch("foundry_client.get_bearer_token_provider", return_value=lambda: "tok"), \
         patch("foundry_client.httpx.AsyncClient", return_value=mock_client):
        from foundry_client import ContentSafetyClient
        client = ContentSafetyClient()
        client._endpoint = "https://fake.cognitiveservices.azure.com"
        is_safe, flagged = await client.is_safe("Any content.")

    assert is_safe is True  # fail-open for availability


@pytest.mark.asyncio
async def test_content_safety_skips_check_when_endpoint_not_configured():
    with patch("foundry_client.DefaultAzureCredential"):
        from foundry_client import ContentSafetyClient
        client = ContentSafetyClient()
        client._endpoint = ""
        is_safe, flagged = await client.is_safe("Anything.")

    assert is_safe is True
    assert flagged == []
