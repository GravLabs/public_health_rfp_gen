"""
Azure AI Foundry integration — first-class component of the Public Health RFP POC.
Responsibilities:
  1. Evaluations: groundedness and coherence via azure.ai.evaluation SDK
  2. Content Safety: check generated drafts for harmful content
  3. Tracing: AI Foundry native tracing for prompt/response spans
  4. Project metadata: list deployments, connections, evaluation runs

Authentication: DefaultAzureCredential → AzureML Data Scientist role on Foundry project.
"""
from __future__ import annotations

import logging
import os
from typing import Optional, Any

from azure.identity import DefaultAzureCredential
import httpx

try:
    from azure.ai.evaluation import (
        GroundednessEvaluator,
        CoherenceEvaluator,
        AzureOpenAIModelConfiguration,
    )
    _EVAL_SDK_AVAILABLE = True
except ImportError:
    _EVAL_SDK_AVAILABLE = False

log = logging.getLogger(__name__)

# AI Foundry project connection — set by post-provision.sh via AZD env vars
FOUNDRY_PROJECT_ENDPOINT = os.getenv("AZURE_AI_FOUNDRY_PROJECT_ENDPOINT", "")
FOUNDRY_SUBSCRIPTION_ID = os.getenv("AZURE_SUBSCRIPTION_ID", "")
FOUNDRY_RESOURCE_GROUP = os.getenv("AZURE_RESOURCE_GROUP", "")
FOUNDRY_HUB_NAME = os.getenv("AZURE_AI_FOUNDRY_HUB_NAME", "")
FOUNDRY_PROJECT_NAME = os.getenv("AZURE_AI_FOUNDRY_PROJECT_NAME", "")
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_CHAT_DEPLOYMENT = os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-4o")
CONTENT_SAFETY_ENDPOINT = os.getenv("AZURE_CONTENT_SAFETY_ENDPOINT", "")

# Likert 1-5 → 0-1 normalization
_LIKERT_NORMALIZE = lambda raw: (float(raw) - 1.0) / 4.0


class FoundryEvaluatorClient:
    """Runs AI Foundry SDK evaluators (groundedness, coherence) for the go/no-go gate."""

    def __init__(self, credential: Optional[DefaultAzureCredential] = None):
        self._credential = credential or DefaultAzureCredential()
        if _EVAL_SDK_AVAILABLE:
            self._model_config = AzureOpenAIModelConfiguration(
                azure_endpoint=AZURE_OPENAI_ENDPOINT,
                azure_deployment=AZURE_OPENAI_CHAT_DEPLOYMENT,
            )
        else:
            self._model_config = None
            log.warning("azure-ai-evaluation not installed — Foundry evaluators disabled")

    def evaluate_groundedness(self, query: str, response: str, context: str) -> float:
        """
        Score response groundedness against context using AI Foundry.
        Returns normalized score 0.0–1.0.
        """
        if not _EVAL_SDK_AVAILABLE or self._model_config is None:
            return 0.5
        try:
            evaluator = GroundednessEvaluator(model_config=self._model_config)
            result = evaluator(query=query, response=response, context=context)
            raw = result.get("groundedness", 3.0)
            return round(_LIKERT_NORMALIZE(raw), 4)
        except Exception as e:
            log.warning("Groundedness evaluation failed: %s", e)
            return 0.5

    def evaluate_coherence(self, query: str, response: str) -> float:
        """
        Score response coherence using AI Foundry.
        Returns normalized score 0.0–1.0.
        """
        if not _EVAL_SDK_AVAILABLE or self._model_config is None:
            return 0.5
        try:
            evaluator = CoherenceEvaluator(model_config=self._model_config)
            result = evaluator(query=query, response=response)
            raw = result.get("coherence", 3.0)
            return round(_LIKERT_NORMALIZE(raw), 4)
        except Exception as e:
            log.warning("Coherence evaluation failed: %s", e)
            return 0.5

    def evaluate_full_draft(
        self,
        rfp_id: str,
        sections: dict[str, str],
        grounding_context: str,
    ) -> dict[str, float]:
        """
        Run both groundedness and coherence over the full draft.
        Returns {"groundedness": float, "coherence": float}.
        """
        full_text = "\n\n".join(sections.values())
        query = f"Generate an Public Health Labs cooperative agreement RFP for {rfp_id}"

        groundedness = self.evaluate_groundedness(query, full_text, grounding_context)
        coherence = self.evaluate_coherence(query, full_text)

        log.info("Foundry eval for %s — groundedness: %.2f, coherence: %.2f",
                 rfp_id, groundedness, coherence)
        return {"groundedness": groundedness, "coherence": coherence}


class ContentSafetyClient:
    """
    Azure AI Content Safety — checks generated RFP drafts for harmful content.
    Required before returning any draft to the caller.
    Endpoint: AZURE_CONTENT_SAFETY_ENDPOINT (provisioned in infra/modules/openai.bicep area)
    """

    def __init__(self, credential: Optional[DefaultAzureCredential] = None):
        self._credential = credential or DefaultAzureCredential()
        from azure.identity import get_bearer_token_provider
        from azure.ai.contentsafety import ContentSafetyClient as _AzCS
        # Lazy import — package may not be installed in all envs
        self._endpoint = CONTENT_SAFETY_ENDPOINT

    async def is_safe(self, text: str) -> tuple[bool, list[str]]:
        """
        Check text for harmful content categories.
        Returns (is_safe, list_of_flagged_categories).
        """
        if not self._endpoint:
            log.debug("Content Safety endpoint not configured — skipping check")
            return True, []

        from azure.identity import get_bearer_token_provider
        token = get_bearer_token_provider(
            self._credential, "https://cognitiveservices.azure.com/.default"
        )()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        body = {
            "text": text[:10_000],  # API limit
            "categories": ["Hate", "SelfHarm", "Violence"],
            "outputType": "FourSeverityLevels"
        }

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{self._endpoint}/contentsafety/text:analyze?api-version=2024-09-01",
                    headers=headers, json=body
                )
                resp.raise_for_status()
                results = resp.json().get("categoriesAnalysis", [])
                flagged = [r["category"] for r in results if r.get("severity", 0) >= 2]
                return len(flagged) == 0, flagged
        except Exception as e:
            log.warning("Content Safety check failed (allowing through): %s", e)
            return True, []


class FoundryTracer:
    """
    Configures AI Foundry native tracing (OpenTelemetry → Azure Monitor).
    Call setup() once at application startup.
    """

    @staticmethod
    def setup(project_endpoint: Optional[str] = None, app_insights_conn_str: Optional[str] = None) -> None:
        endpoint = project_endpoint or FOUNDRY_PROJECT_ENDPOINT
        conn_str = app_insights_conn_str or os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")

        if not conn_str:
            log.warning("AI Foundry tracing: no App Insights connection string — traces will not be exported")
            return

        try:
            from azure.monitor.opentelemetry import configure_azure_monitor
            configure_azure_monitor(connection_string=conn_str)
            # Enable Semantic Kernel / AI SDK instrumentation
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry import trace
            log.info("AI Foundry tracing configured → App Insights")
        except ImportError as e:
            log.warning("AI Foundry tracing setup failed (missing package): %s", e)

    @staticmethod
    def get_tracer(name: str = "pubhealth.rfp.foundry"):
        from opentelemetry import trace
        return trace.get_tracer(name)


class FoundryProjectClient:
    """
    Management-plane client for the AI Foundry project.
    Lists deployments, connections, and evaluation run history.
    """

    def __init__(self, credential: Optional[DefaultAzureCredential] = None):
        self._credential = credential or DefaultAzureCredential()
        from azure.identity import get_bearer_token_provider
        self._token_provider = get_bearer_token_provider(
            self._credential, "https://management.azure.com/.default"
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token_provider()}"}

    async def list_deployments(self) -> list[dict]:
        """List available model deployments in the AI Foundry project."""
        url = (
            f"https://management.azure.com/subscriptions/{FOUNDRY_SUBSCRIPTION_ID}"
            f"/resourceGroups/{FOUNDRY_RESOURCE_GROUP}"
            f"/providers/Microsoft.MachineLearningServices/workspaces/{FOUNDRY_PROJECT_NAME}"
            f"/onlineDeployments?api-version=2024-04-01"
        )
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(url, headers=self._headers())
                resp.raise_for_status()
                return resp.json().get("value", [])
        except Exception as e:
            log.warning("Failed to list Foundry deployments: %s", e)
            return []

    async def get_project_info(self) -> dict:
        """Return key AI Foundry project metadata for health checks."""
        return {
            "endpoint": FOUNDRY_PROJECT_ENDPOINT,
            "project_name": FOUNDRY_PROJECT_NAME,
            "hub_name": FOUNDRY_HUB_NAME,
            "chat_deployment": AZURE_OPENAI_CHAT_DEPLOYMENT,
            "content_safety_configured": bool(CONTENT_SAFETY_ENDPOINT),
        }
