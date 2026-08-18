"""
Coherence evaluator — direct Azure OpenAI call, no SDK dependency.
Scores logical flow and clarity of the generated text (1–5 Likert → 0–1).
Auth: uses AZURE_OPENAI_API_KEY if set, otherwise DefaultAzureCredential (managed identity).
"""

import os
import json
import httpx

AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "")
MINI_DEPLOYMENT = os.getenv("AZURE_OPENAI_MINI_DEPLOYMENT", "gpt-4o-mini")
API_VERSION = "2024-06-01"

_SYSTEM = (
    "You are an evaluation assistant. Score the coherence of the TEXT — "
    "logical flow, clarity, and structure — on a scale of 1 (incoherent) to 5 (highly coherent). "
    "Reply with JSON only: {\"score\": <int 1-5>, \"reason\": \"<one sentence>\"}."
)


def _auth_header() -> dict:
    api_key = os.getenv("AZURE_OPENAI_API_KEY")
    if api_key:
        return {"api-key": api_key}
    from azure.identity import DefaultAzureCredential
    token = DefaultAzureCredential().get_token("https://cognitiveservices.azure.com/.default").token
    return {"Authorization": f"Bearer {token}"}


def score_coherence(generated_text: str) -> float:
    snippet = generated_text[:3000]

    url = (
        f"{AZURE_OPENAI_ENDPOINT.rstrip('/')}/openai/deployments/"
        f"{MINI_DEPLOYMENT}/chat/completions?api-version={API_VERSION}"
    )
    payload = {
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": f"TEXT:\n{snippet}"},
        ],
        "temperature": 0,
        "max_tokens": 100,
        "response_format": {"type": "json_object"},
    }

    with httpx.Client(timeout=30) as client:
        resp = client.post(url, json=payload, headers=_auth_header())
        resp.raise_for_status()

    content = resp.json()["choices"][0]["message"]["content"]
    raw = json.loads(content).get("score", 3)
    return (float(raw) - 1.0) / 4.0
