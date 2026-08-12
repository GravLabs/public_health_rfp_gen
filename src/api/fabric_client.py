"""
Microsoft Fabric integration via REST API.
Manages: Workspace, Lakehouse (OneLake storage), AI Skill, and evaluation telemetry.
Uses DefaultAzureCredential — requires Fabric Workspace Admin or Contributor role.

Fabric REST API reference: https://api.fabric.microsoft.com/v1
Fabric trial: https://learn.microsoft.com/fabric/get-started/fabric-trial
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional, Any

import httpx
from azure.identity import DefaultAzureCredential, get_bearer_token_provider

log = logging.getLogger(__name__)

FABRIC_BASE = "https://api.fabric.microsoft.com/v1"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
ONELAKE_BASE = "https://onelake.dfs.fabric.microsoft.com"


class FabricClient:
    def __init__(
        self,
        workspace_id: str,
        lakehouse_id: str,
        credential: Optional[DefaultAzureCredential] = None,
    ):
        self._workspace_id = workspace_id
        self._lakehouse_id = lakehouse_id
        self._credential = credential or DefaultAzureCredential()
        self._token_provider = get_bearer_token_provider(self._credential, FABRIC_SCOPE)
        self._onelake_token_provider = get_bearer_token_provider(
            self._credential, "https://storage.azure.com/.default"
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token_provider()}", "Content-Type": "application/json"}

    def _onelake_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._onelake_token_provider()}", "x-ms-version": "2020-10-02"}

    # ── Workspace and Lakehouse setup ─────────────────────────────────────────

    @classmethod
    async def provision_workspace(cls, workspace_name: str, credential: Optional[DefaultAzureCredential] = None) -> str:
        """Create a Fabric workspace. Returns workspace ID."""
        cred = credential or DefaultAzureCredential()
        token = get_bearer_token_provider(cred, FABRIC_SCOPE)()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{FABRIC_BASE}/workspaces",
                headers=headers,
                json={"displayName": workspace_name, "description": "Public Health RFP Generation POC"}
            )
            resp.raise_for_status()
        workspace_id = resp.json()["id"]
        log.info("Fabric workspace created: %s (%s)", workspace_name, workspace_id)
        return workspace_id

    @classmethod
    async def provision_lakehouse(cls, workspace_id: str, lakehouse_name: str, credential: Optional[DefaultAzureCredential] = None) -> str:
        """Create a Fabric Lakehouse. Returns lakehouse ID."""
        cred = credential or DefaultAzureCredential()
        token = get_bearer_token_provider(cred, FABRIC_SCOPE)()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{FABRIC_BASE}/workspaces/{workspace_id}/lakehouses",
                headers=headers,
                json={"displayName": lakehouse_name}
            )
            resp.raise_for_status()
        lakehouse_id = resp.json()["id"]
        log.info("Fabric Lakehouse created: %s (%s)", lakehouse_name, lakehouse_id)
        return lakehouse_id

    # ── OneLake file operations ───────────────────────────────────────────────

    async def upload_rfp_document(self, file_name: str, content: bytes, folder: str = "Files/rfp-corpus") -> str:
        """Upload an RFP document to OneLake via ADLS Gen2-compatible API."""
        path = f"{self._workspace_id}/{self._lakehouse_id}/{folder}/{file_name}"
        url = f"{ONELAKE_BASE}/{path}"

        async with httpx.AsyncClient(timeout=120) as client:
            # Create file
            create_resp = await client.put(
                url, headers={**self._onelake_headers(), "x-ms-resource": "file"}
            )
            create_resp.raise_for_status()

            # Append content
            append_resp = await client.patch(
                f"{url}?action=append&position=0",
                headers={**self._onelake_headers(), "Content-Type": "application/octet-stream"},
                content=content
            )
            append_resp.raise_for_status()

            # Flush
            flush_resp = await client.patch(
                f"{url}?action=flush&position={len(content)}",
                headers=self._onelake_headers()
            )
            flush_resp.raise_for_status()

        onelake_path = f"onelake://{self._workspace_id}/{self._lakehouse_id}/{folder}/{file_name}"
        log.info("Uploaded to OneLake: %s", onelake_path)
        return onelake_path

    async def write_eval_record(self, eval_data: dict[str, Any]) -> str:
        """Write an evaluation result as a JSON record to the Lakehouse Tables layer."""
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        file_name = f"eval_{eval_data.get('draft_id', 'unknown')}_{timestamp}.json"
        content = json.dumps(eval_data, indent=2).encode()

        return await self.upload_rfp_document(file_name, content, folder="Files/eval-results")

    async def write_draft_to_lakehouse(self, draft_id: str, rfp_id: str, content_md: str) -> str:
        """Write a generated RFP draft to OneLake for archival and Power BI lineage."""
        file_name = f"{rfp_id}_{draft_id}.md"
        return await self.upload_rfp_document(file_name, content_md.encode(), folder="Files/generated-drafts")

    # ── AI Skill ──────────────────────────────────────────────────────────────

    @classmethod
    async def provision_ai_skill(
        cls,
        workspace_id: str,
        skill_name: str,
        ai_search_endpoint: str,
        ai_search_index: str,
        credential: Optional[DefaultAzureCredential] = None,
    ) -> str:
        """Create a Fabric AI Skill backed by Azure AI Search. Returns skill ID."""
        cred = credential or DefaultAzureCredential()
        token = get_bearer_token_provider(cred, FABRIC_SCOPE)()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        skill_definition = {
            "displayName": skill_name,
            "type": "AISkill",
            "definition": {
                "parts": [{
                    "type": "AISkillDefinition",
                    "payload": json.dumps({
                        "schemaVersion": 1,
                        "instruction": (
                            "You are an expert on Public Health Labs public health laboratory RFPs and CDC cooperative agreements. "
                            "Answer questions about funding opportunities, eligibility requirements, and program scopes "
                            "based only on the indexed RFP corpus. Always cite the RFP ID when referencing specific content."
                        ),
                        "dataSources": [{
                            "type": "AzureAISearch",
                            "endpoint": ai_search_endpoint,
                            "indexName": ai_search_index,
                            "authentication": {"type": "ManagedIdentity"}
                        }]
                    })
                }]
            }
        }

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{FABRIC_BASE}/workspaces/{workspace_id}/items",
                headers=headers,
                json=skill_definition
            )
            resp.raise_for_status()

        skill_id = resp.json()["id"]
        log.info("Fabric AI Skill created: %s (%s)", skill_name, skill_id)
        return skill_id

    # ── Fabric Pipeline ───────────────────────────────────────────────────────

    @classmethod
    async def provision_ingestion_pipeline(
        cls,
        workspace_id: str,
        pipeline_name: str,
        sharepoint_site_id: str,
        lakehouse_id: str,
        credential: Optional[DefaultAzureCredential] = None,
    ) -> str:
        """Create a Fabric Data Pipeline for SharePoint → OneLake ingestion."""
        cred = credential or DefaultAzureCredential()
        token = get_bearer_token_provider(cred, FABRIC_SCOPE)()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        pipeline_def = {
            "displayName": pipeline_name,
            "type": "DataPipeline",
            "definition": {
                "parts": [{
                    "type": "pipeline",
                    "payload": json.dumps({
                        "name": pipeline_name,
                        "properties": {
                            "activities": [{
                                "name": "CopySharePointToLakehouse",
                                "type": "Copy",
                                "typeProperties": {
                                    "source": {
                                        "type": "SharePointOnlineListSource",
                                        "siteUrl": f"https://graph.microsoft.com/v1.0/sites/{sharepoint_site_id}",
                                        "listName": "RFP Corpus"
                                    },
                                    "sink": {
                                        "type": "LakehouseTableSink",
                                        "workspaceId": workspace_id,
                                        "artifactId": lakehouse_id,
                                        "rootFolder": "Files/rfp-corpus"
                                    }
                                }
                            }]
                        }
                    })
                }]
            }
        }

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{FABRIC_BASE}/workspaces/{workspace_id}/items",
                headers=headers,
                json=pipeline_def
            )
            resp.raise_for_status()

        pipeline_id = resp.json()["id"]
        log.info("Fabric Pipeline created: %s (%s)", pipeline_name, pipeline_id)
        return pipeline_id
