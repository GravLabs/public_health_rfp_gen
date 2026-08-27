"""
Groundedness evaluator — uses the Azure AI Foundry Evaluation SDK
(azure-ai-evaluation) directly, not a hand-rolled REST call.

Evaluates whether the RFP content is grounded in (consistent with) the
stated input parameters (program area, sponsor, funding, period) — not
retrieved document chunks, so a low score here is informational, not
necessarily a draft-quality problem (see gate.py's comment on this).

Auth: DefaultAzureCredential (managed identity) — no API keys.
"""

import os

from azure.ai.evaluation import AzureOpenAIModelConfiguration, GroundednessEvaluator
from azure.identity import DefaultAzureCredential

AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "")
MINI_DEPLOYMENT = os.getenv("AZURE_OPENAI_MINI_DEPLOYMENT", "gpt-4o-mini")

_model_config = AzureOpenAIModelConfiguration(
    azure_endpoint=AZURE_OPENAI_ENDPOINT,
    azure_deployment=MINI_DEPLOYMENT,
)
_evaluator = GroundednessEvaluator(model_config=_model_config, credential=DefaultAzureCredential())


def score_groundedness(generated_text: str, input_spec: dict) -> float:
    """Likert 1-5 groundedness from the SDK evaluator, normalized to 0-1."""
    params_summary = ", ".join(
        f"{k}={v}" for k, v in input_spec.items()
        if k not in ("_grounding_context",) and v is not None
    )
    result = _evaluator(
        query="Generate a federal public health cooperative agreement RFP consistent with these parameters.",
        response=generated_text[:3000],
        context=f"Program parameters: {params_summary}",
    )
    raw = result.get("groundedness", 3.0)
    return (float(raw) - 1.0) / 4.0
