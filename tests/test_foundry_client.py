"""Unit tests for FoundryEvaluatorClient and ContentSafetyClient."""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "api"))


def test_foundry_evaluator_normalizes_groundedness():
    from foundry_client import _LIKERT_NORMALIZE
    assert _LIKERT_NORMALIZE(1.0) == 0.0
    assert _LIKERT_NORMALIZE(3.0) == 0.5
    assert _LIKERT_NORMALIZE(5.0) == 1.0


def test_evaluate_groundedness_returns_normalized_score():
    with patch("foundry_client.DefaultAzureCredential"), \
         patch("foundry_client.AzureOpenAIModelConfiguration"), \
         patch("foundry_client.GroundednessEvaluator") as mock_gnd:
        mock_gnd.return_value = MagicMock(return_value={"groundedness": 4.0})

        from foundry_client import FoundryEvaluatorClient
        client = FoundryEvaluatorClient()
        score = client.evaluate_groundedness("query", "response", "context")

    # (4.0 - 1.0) / 4.0 = 0.75
    assert abs(score - 0.75) < 1e-6


def test_evaluate_groundedness_returns_neutral_on_exception():
    with patch("foundry_client.DefaultAzureCredential"), \
         patch("foundry_client.AzureOpenAIModelConfiguration"), \
         patch("foundry_client.GroundednessEvaluator") as mock_gnd:
        mock_gnd.return_value = MagicMock(side_effect=Exception("API error"))

        from foundry_client import FoundryEvaluatorClient
        client = FoundryEvaluatorClient()
        score = client.evaluate_groundedness("q", "r", "c")

    assert score == 0.5  # neutral fallback, not 0.0 or 1.0


def test_evaluate_coherence_returns_normalized_score():
    with patch("foundry_client.DefaultAzureCredential"), \
         patch("foundry_client.AzureOpenAIModelConfiguration"), \
         patch("foundry_client.CoherenceEvaluator") as mock_coh:
        mock_coh.return_value = MagicMock(return_value={"coherence": 5.0})

        from foundry_client import FoundryEvaluatorClient
        client = FoundryEvaluatorClient()
        score = client.evaluate_coherence("query", "response")

    assert score == 1.0


def test_evaluate_full_draft_returns_both_scores():
    with patch("foundry_client.DefaultAzureCredential"), \
         patch("foundry_client.AzureOpenAIModelConfiguration"), \
         patch("foundry_client.GroundednessEvaluator") as gnd, \
         patch("foundry_client.CoherenceEvaluator") as coh:
        gnd.return_value = MagicMock(return_value={"groundedness": 4.0})
        coh.return_value = MagicMock(return_value={"coherence": 3.0})

        from foundry_client import FoundryEvaluatorClient
        client = FoundryEvaluatorClient()
        result = client.evaluate_full_draft(
            rfp_id="Public Health Labs-RFP-TEST",
            sections={"background": "Test background text.", "scope_of_work": "Do the work."},
            grounding_context="Context from corpus.",
        )

    assert "groundedness" in result
    assert "coherence" in result
    assert abs(result["groundedness"] - 0.75) < 1e-6
    assert abs(result["coherence"] - 0.5) < 1e-6


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
         patch("foundry_client.get_bearer_token_provider", return_value=lambda _: lambda: "tok"), \
         patch("foundry_client.httpx.AsyncClient", return_value=mock_client):
        from foundry_client import ContentSafetyClient
        client = ContentSafetyClient()
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
         patch("foundry_client.CONTENT_SAFETY_ENDPOINT", "https://fake.cognitiveservices.azure.com"), \
         patch("foundry_client.get_bearer_token_provider", return_value=lambda _: lambda: "tok"), \
         patch("foundry_client.httpx.AsyncClient", return_value=mock_client):
        from foundry_client import ContentSafetyClient
        client = ContentSafetyClient()
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
         patch("foundry_client.CONTENT_SAFETY_ENDPOINT", "https://fake.cognitiveservices.azure.com"), \
         patch("foundry_client.get_bearer_token_provider", return_value=lambda _: lambda: "tok"), \
         patch("foundry_client.httpx.AsyncClient", return_value=mock_client):
        from foundry_client import ContentSafetyClient
        client = ContentSafetyClient()
        is_safe, flagged = await client.is_safe("Any content.")

    assert is_safe is True  # fail-open for availability
