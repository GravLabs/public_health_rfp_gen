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
    async def provision_workspace(
        cls, workspace_name: str, credential: Optional[DefaultAzureCredential] = None,
        capacity_id: Optional[str] = None,
    ) -> str:
        """Create a Fabric workspace and assign it to a capacity. Returns workspace ID.

        A workspace with no capacity assigned cannot host items (lakehouses,
        notebooks, etc.) — the Fabric API 403s on item creation with no useful
        error body. capacity_id must be resolved by the caller (see
        `find_trial_capacity_id`) since a tenant can have multiple capacities
        and there's no single correct default to assume here.
        """
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

            if capacity_id:
                assign_resp = await client.post(
                    f"{FABRIC_BASE}/workspaces/{workspace_id}/assignToCapacity",
                    headers=headers,
                    json={"capacityId": capacity_id}
                )
                assign_resp.raise_for_status()
                log.info("Fabric workspace %s assigned to capacity %s", workspace_id, capacity_id)

        return workspace_id

    @classmethod
    async def grant_workspace_role(
        cls, workspace_id: str, principal_id: str, role: str = "Contributor",
        principal_type: str = "ServicePrincipal", credential: Optional[DefaultAzureCredential] = None,
    ) -> None:
        """Grant a principal (e.g. the API's managed identity) a Fabric workspace
        role. Fabric workspace access is Fabric-native role-based (Admin/Member/
        Contributor/Viewer), entirely separate from Azure RBAC — an Azure
        'Contributor' role on the resource group does not grant OneLake access.
        Without this, every write (write_draft_to_lakehouse, write_eval_record)
        403s at the OneLake layer regardless of any Azure-side permissions."""
        cred = credential or DefaultAzureCredential()
        token = get_bearer_token_provider(cred, FABRIC_SCOPE)()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{FABRIC_BASE}/workspaces/{workspace_id}/roleAssignments",
                headers=headers,
                json={"principal": {"id": principal_id, "type": principal_type}, "role": role}
            )
            resp.raise_for_status()
        log.info("Fabric workspace %s: granted %s role to %s %s", workspace_id, role, principal_type, principal_id)

    @classmethod
    async def find_trial_capacity_id(cls, credential: Optional[DefaultAzureCredential] = None) -> str:
        """Find an active Fabric trial capacity (sku starting with 'FT') in the tenant.
        Raises if none or more than one is found — ambiguous cases need an explicit --capacity-id."""
        cred = credential or DefaultAzureCredential()
        token = get_bearer_token_provider(cred, FABRIC_SCOPE)()
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{FABRIC_BASE}/capacities", headers=headers)
            resp.raise_for_status()
        capacities = resp.json().get("value", [])
        trials = [c for c in capacities if c.get("sku", "").startswith("FT") and c.get("state") == "Active"]
        if not trials:
            raise RuntimeError(
                f"No active Fabric trial capacity found among {len(capacities)} capacities. "
                "Activate one at https://app.fabric.microsoft.com, or pass --capacity-id explicitly."
            )
        if len(trials) > 1:
            ids = ", ".join(f"{c['displayName']} ({c['id']})" for c in trials)
            raise RuntimeError(f"Multiple active trial capacities found — pass --capacity-id explicitly: {ids}")
        return trials[0]["id"]

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

    async def upload_rfp_document(self, file_name: str, content: bytes, folder: str = "rfp-corpus") -> str:
        """Upload an RFP document to OneLake via ADLS Gen2-compatible API.

        `folder` is relative to the Lakehouse's own reserved `Files/` root —
        do not include a leading "Files/" here, the OneLake path already is
        `{workspace}/{lakehouse}/Files/...`; prepending it again produces a
        redundant `Files/Files/...` nesting (confirmed by listing the live
        lakehouse after a real write)."""
        path = f"{self._workspace_id}/{self._lakehouse_id}/Files/{folder}/{file_name}"
        url = f"{ONELAKE_BASE}/{path}"

        async with httpx.AsyncClient(timeout=120) as client:
            # Create file. `resource=file` must be a query param, not a header —
            # sending it as `x-ms-resource` (the previous code here) gets a
            # generic "mandatory header not specified" 400 with no detail on
            # which header, confirmed against the live OneLake DFS endpoint.
            # The empty-body create/flush calls also need an explicit
            # Content-Length: 0 — httpx does not add it automatically.
            create_resp = await client.put(
                f"{url}?resource=file",
                headers={**self._onelake_headers(), "Content-Length": "0"},
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
                headers={**self._onelake_headers(), "Content-Length": "0"},
            )
            flush_resp.raise_for_status()

        onelake_path = f"onelake://{self._workspace_id}/{self._lakehouse_id}/Files/{folder}/{file_name}"
        log.info("Uploaded to OneLake: %s", onelake_path)
        return onelake_path

    async def write_eval_record(self, eval_data: dict[str, Any]) -> str:
        """Write an evaluation result as a JSON record to the Lakehouse Tables layer."""
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        file_name = f"eval_{eval_data.get('draft_id', 'unknown')}_{timestamp}.json"
        content = json.dumps(eval_data, indent=2).encode()

        return await self.upload_rfp_document(file_name, content, folder="eval-results")

    async def write_draft_to_lakehouse(self, draft_id: str, rfp_id: str, content_md: str) -> str:
        """Write a generated RFP draft to OneLake for archival and Power BI lineage."""
        file_name = f"{rfp_id}_{draft_id}.md"
        return await self.upload_rfp_document(file_name, content_md.encode(), folder="generated-drafts")

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
