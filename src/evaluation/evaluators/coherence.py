"""
Coherence evaluator via Azure AI Foundry Evaluation SDK.
"""

import os
from azure.ai.evaluation import CoherenceEvaluator

# Routes through APIM using gpt-4o-mini — coherence scoring is a structured rubric task.
APIM_ENDPOINT = os.getenv("AZURE_APIM_GATEWAY_URL")
APIM_EVAL_KEY = os.getenv("AZURE_APIM_EVALUATION_KEY")
MINI_DEPLOYMENT = os.getenv("AZURE_OPENAI_MINI_DEPLOYMENT", "gpt-4o-mini")


def score_coherence(generated_text: str) -> float:
    model_config = {
        "azure_endpoint": APIM_ENDPOINT,
        "azure_deployment": MINI_DEPLOYMENT,
        "api_version": "2024-06-01",
        "api_key": APIM_EVAL_KEY,
    }

    evaluator = CoherenceEvaluator(model_config=model_config)
    result = evaluator(response=generated_text)

    raw = result.get("coherence", 3.0)
    return (float(raw) - 1.0) / 4.0
