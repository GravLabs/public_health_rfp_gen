"""
Coherence evaluator — uses the Azure AI Foundry Evaluation SDK
(azure-ai-evaluation) directly, not a hand-rolled REST call.

Scores logical flow and clarity of the generated text (1-5 Likert -> 0-1).
Auth: DefaultAzureCredential (managed identity) — no API keys.
"""

import os

from azure.ai.evaluation import AzureOpenAIModelConfiguration, CoherenceEvaluator
from azure.identity import DefaultAzureCredential

AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "")
MINI_DEPLOYMENT = os.getenv("AZURE_OPENAI_MINI_DEPLOYMENT", "gpt-4o-mini")

_model_config = AzureOpenAIModelConfiguration(
    azure_endpoint=AZURE_OPENAI_ENDPOINT,
    azure_deployment=MINI_DEPLOYMENT,
)
_evaluator = CoherenceEvaluator(model_config=_model_config, credential=DefaultAzureCredential())


def score_coherence(generated_text: str) -> float:
    """Likert 1-5 coherence from the SDK evaluator, normalized to 0-1."""
    result = _evaluator(
        query="Generate a federal public health cooperative agreement RFP.",
        response=generated_text[:3000],
    )
    raw = result.get("coherence", 3.0)
    return (float(raw) - 1.0) / 4.0
