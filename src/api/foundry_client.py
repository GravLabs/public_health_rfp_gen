"""
Azure AI Foundry integration — first-class component of the Public Health RFP POC.
Responsibilities:
  1. Content Safety: check generated drafts for harmful content
  2. Tracing: AI Foundry native tracing for prompt/response spans
  3. Project metadata: list deployments, connections, evaluation runs

Groundedness/coherence evaluation lives in src/evaluation/evaluators/ instead
(it needs to run there regardless of which service calls the gate, and shares
the same azure-ai-evaluation SDK usage pattern this file used to duplicate).

Authentication: DefaultAzureCredential → Cognitive Services OpenAI User role on
the AI Foundry resource.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
import httpx

log = logging.getLogger(__name__)

# AI Foundry project connection — set by post-provision.sh via AZD env vars
FOUNDRY_PROJECT_ENDPOINT = os.getenv("AZURE_AI_FOUNDRY_PROJECT_ENDPOINT", "")
FOUNDRY_SUBSCRIPTION_ID = os.getenv("AZURE_SUBSCRIPTION_ID", "")
FOUNDRY_RESOURCE_GROUP = os.getenv("AZURE_RESOURCE_GROUP", "")
FOUNDRY_PROJECT_NAME = os.getenv("AZURE_AI_FOUNDRY_PROJECT_NAME", "")
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_CHAT_DEPLOYMENT = os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-4o")
CONTENT_SAFETY_ENDPOINT = os.getenv("AZURE_CONTENT_SAFETY_ENDPOINT", "")


class ContentSafetyClient:
    """
    Azure AI Content Safety — checks generated RFP drafts for harmful content.
    Required before returning any draft to the caller.
    Endpoint: AZURE_CONTENT_SAFETY_ENDPOINT — the same unified AI Foundry
    resource as AZURE_OPENAI_ENDPOINT (infra/modules/foundry.bicep); an
    AIServices account serves /contentsafety/* alongside /openai/*, no
    separate resource needed.
    """

    def __init__(self, credential: Optional[DefaultAzureCredential] = None):
        self._credential = credential or DefaultAzureCredential()
        self._endpoint = CONTENT_SAFETY_ENDPOINT

    async def is_safe(self, text: str) -> tuple[bool, list[str]]:
        """
        Check text for harmful content categories.
        Returns (is_safe, list_of_flagged_categories).
        """
        if not self._endpoint:
            log.debug("Content Safety endpoint not configured — skipping check")
            return True, []

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
            "chat_deployment": AZURE_OPENAI_CHAT_DEPLOYMENT,
            "content_safety_configured": bool(CONTENT_SAFETY_ENDPOINT),
        }
