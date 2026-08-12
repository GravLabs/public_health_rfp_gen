"""
Groundedness evaluator via Azure AI Foundry Evaluation SDK.
Measures whether generated text is supported by the retrieved source documents.
"""

import os
from azure.ai.evaluation import GroundednessEvaluator

# Routes through APIM using gpt-4o-mini — structured scoring task, no need for full GPT-4o.
# APIM subscription key (sub-evaluation) is the auth mechanism; APIM authenticates to
# Azure OpenAI via SystemAssigned managed identity so no OpenAI key is exposed here.
APIM_ENDPOINT = os.getenv("AZURE_APIM_GATEWAY_URL")
APIM_EVAL_KEY = os.getenv("AZURE_APIM_EVALUATION_KEY")
MINI_DEPLOYMENT = os.getenv("AZURE_OPENAI_MINI_DEPLOYMENT", "gpt-4o-mini")


def score_groundedness(generated_text: str, input_spec: dict) -> float:
    """
    Returns a 0.0–1.0 groundedness score.
    context is constructed from the input spec (which the retriever used as the query basis).
    For production, pass retrieved document chunks as context instead.
    """
    model_config = {
        "azure_endpoint": APIM_ENDPOINT,
        "azure_deployment": MINI_DEPLOYMENT,
        "api_version": "2024-06-01",
        "api_key": APIM_EVAL_KEY,
    }

    evaluator = GroundednessEvaluator(model_config=model_config)

    # Build a context string from the input spec for grounding reference
    context = "\n".join(f"{k}: {v}" for k, v in input_spec.items() if isinstance(v, str))

    result = evaluator(
        response=generated_text,
        context=context,
    )

    # AI Foundry returns score as 1–5 Likert; normalize to 0–1
    raw = result.get("groundedness", 3.0)
    return (float(raw) - 1.0) / 4.0
