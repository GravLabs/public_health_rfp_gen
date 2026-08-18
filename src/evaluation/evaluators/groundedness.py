"""
Groundedness evaluator — direct Azure OpenAI call, no SDK dependency.
Evaluates whether the RFP content is factually consistent with federal grant norms
and the stated input parameters (program area, sponsor, funding, period).
Auth: uses AZURE_OPENAI_API_KEY if set, otherwise DefaultAzureCredential (managed identity).
"""

import os
import json
import httpx

AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "")
MINI_DEPLOYMENT = os.getenv("AZURE_OPENAI_MINI_DEPLOYMENT", "gpt-4o-mini")
API_VERSION = "2024-06-01"

_SYSTEM = (
    "You are a federal grant specialist evaluating an AI-generated RFP. "
    "Score the RFP on how factually grounded it is — meaning: accurate use of federal grant terminology, "
    "regulatory citations (2 CFR 200, CLIA, etc.), and consistency with the stated program parameters. "
    "Scale: 1 = fabricated or inaccurate content, 5 = fully grounded in federal grant norms. "
    "Reply with JSON only: {\"score\": <int 1-5>, \"reason\": \"<one sentence>\"}."
)


def _auth_header() -> dict:
    api_key = os.getenv("AZURE_OPENAI_API_KEY")
    if api_key:
        return {"api-key": api_key}
    from azure.identity import DefaultAzureCredential
    token = DefaultAzureCredential().get_token("https://cognitiveservices.azure.com/.default").token
    return {"Authorization": f"Bearer {token}"}


def score_groundedness(generated_text: str, input_spec: dict) -> float:
    params_summary = ", ".join(
        f"{k}={v}" for k, v in input_spec.items()
        if k not in ("_grounding_context",) and v is not None
    )
    snippet = generated_text[:3000]

    url = (
        f"{AZURE_OPENAI_ENDPOINT.rstrip('/')}/openai/deployments/"
        f"{MINI_DEPLOYMENT}/chat/completions?api-version={API_VERSION}"
    )
    payload = {
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {
                "role": "user",
                "content": (
                    f"PROGRAM PARAMETERS: {params_summary}\n\n"
                    f"RFP CONTENT:\n{snippet}"
                ),
            },
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
