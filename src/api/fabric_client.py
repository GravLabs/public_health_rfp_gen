"""
Microsoft Fabric integration via REST API.
Manages: Workspace, Lakehouse (OneLake storage), ingestion pipeline, and evaluation telemetry.
Uses DefaultAzureCredential — requires Fabric Workspace Admin or Contributor role.

Fabric REST API reference: https://api.fabric.microsoft.com/v1
Fabric trial: https://learn.microsoft.com/fabric/get-started/fabric-trial
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
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
    async def find_workspace_by_name(cls, workspace_name: str, credential: Optional[DefaultAzureCredential] = None) -> Optional[str]:
        """Return the ID of an existing workspace with this display name, or
        None. Used to make re-provisioning idempotent — without this,
        re-running setup after a teardown would create a second,
        differently-ID'd workspace every time instead of reusing the one
        already there (or noticing it's actually gone, per find-then-create
        in `provision`)."""
        cred = credential or DefaultAzureCredential()
        token = get_bearer_token_provider(cred, FABRIC_SCOPE)()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{FABRIC_BASE}/workspaces", headers={"Authorization": f"Bearer {token}"})
            resp.raise_for_status()
        for ws in resp.json().get("value", []):
            if ws.get("displayName") == workspace_name:
                return ws["id"]
        return None

    @classmethod
    async def find_item_by_name(
        cls, workspace_id: str, item_name: str, item_type: str, credential: Optional[DefaultAzureCredential] = None,
    ) -> Optional[str]:
        """Return the ID of an existing item (Lakehouse, CopyJob, ...) with
        this display name and type inside the workspace, or None."""
        cred = credential or DefaultAzureCredential()
        token = get_bearer_token_provider(cred, FABRIC_SCOPE)()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{FABRIC_BASE}/workspaces/{workspace_id}/items",
                headers={"Authorization": f"Bearer {token}"},
                params={"type": item_type},
            )
            resp.raise_for_status()
        for item in resp.json().get("value", []):
            if item.get("displayName") == item_name:
                return item["id"]
        return None

    @classmethod
    async def find_connection_by_name(cls, connection_name: str, credential: Optional[DefaultAzureCredential] = None) -> Optional[str]:
        """Return the ID of an existing Fabric connection with this display
        name, or None."""
        cred = credential or DefaultAzureCredential()
        token = get_bearer_token_provider(cred, FABRIC_SCOPE)()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{FABRIC_BASE}/connections", headers={"Authorization": f"Bearer {token}"})
            resp.raise_for_status()
        for conn in resp.json().get("value", []):
            if conn.get("displayName") == connection_name:
                return conn["id"]
        return None

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
    async def provision_workspace_identity(
        cls, workspace_id: str, credential: Optional[DefaultAzureCredential] = None,
        poll_attempts: int = 6, poll_interval_s: float = 5.0,
    ) -> dict:
        """Provision the workspace's own Fabric-managed identity — needed for
        the SharePoint connector's WorkspaceIdentity credential type (see
        create_sharepoint_connection). Idempotent in effect: if one already
        exists, returns it via a plain GET rather than erroring, since the
        create call 400s with WorkspaceIdentityAlreadyExists on retry.

        provisionIdentity returns 200 before the identity is actually visible
        on a GET of the workspace -- confirmed live (reproduced twice): an
        immediate GET right after a 200 POST comes back without the
        `workspaceIdentity` key at all, raising KeyError, even though the
        identity does show up moments later. Polls briefly rather than
        failing on the very next line after what the API just told us
        succeeded."""
        cred = credential or DefaultAzureCredential()
        token = get_bearer_token_provider(cred, FABRIC_SCOPE)()
        headers = {"Authorization": f"Bearer {token}", "Content-Length": "0"}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{FABRIC_BASE}/workspaces/{workspace_id}/provisionIdentity", headers=headers)
            if resp.status_code >= 400 and "WorkspaceIdentityAlreadyExists" not in resp.text:
                resp.raise_for_status()
            for attempt in range(poll_attempts):
                get_resp = await client.get(f"{FABRIC_BASE}/workspaces/{workspace_id}", headers={"Authorization": f"Bearer {token}"})
                get_resp.raise_for_status()
                workspace_identity = get_resp.json().get("workspaceIdentity")
                if workspace_identity:
                    identity = workspace_identity
                    break
                if attempt < poll_attempts - 1:
                    await asyncio.sleep(poll_interval_s)
            else:
                raise RuntimeError(
                    f"Workspace {workspace_id}: provisionIdentity succeeded but workspaceIdentity "
                    f"still wasn't visible after {poll_attempts * poll_interval_s:.0f}s of polling."
                )
        log.info("Workspace %s identity: appId=%s spId=%s", workspace_id, identity["applicationId"], identity["servicePrincipalId"])
        return identity

    # Well-known Microsoft first-party resource + Sites.Selected app role IDs —
    # stable, documented values, not worth a lookup call each run. Confirmed
    # live this session. Sites.Read.All/ReadWrite.All do NOT satisfy the
    # SharePoint connector (confirmed via a live 400) — must be Sites.Selected.
    _GRAPH_RESOURCE_APP_ID = "00000003-0000-0000-c000-000000000000"
    _SPO_RESOURCE_APP_ID = "00000003-0000-0ff1-ce00-000000000000"
    _SITES_SELECTED_ROLE_GRAPH = "883ea226-0bf2-4a8f-9f9d-92c9162a727d"
    _SITES_SELECTED_ROLE_SPO = "d13f72ca-a275-4b96-b789-48ebcc4da984"

    @classmethod
    async def grant_sharepoint_access_role(cls, principal_id: str, credential: Optional[DefaultAzureCredential] = None) -> None:
        """Grant Sites.Selected (Graph + SharePoint Online application
        permissions) to a service principal — step 1 of 2 for SharePoint
        access (step 2 is grant_site_permission, the mandatory site-level
        grant; Sites.Selected alone grants access to zero sites). Uses the
        caller's own credential — ordinary Application.ReadWrite.All-level
        directory access is enough for this step, unlike grant_site_permission
        which needs a delegated Sites.FullControl.All token."""
        cred = credential or DefaultAzureCredential()
        token = get_bearer_token_provider(cred, "https://graph.microsoft.com/.default")()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=30) as client:
            for resource_app_id, role_id in (
                (cls._GRAPH_RESOURCE_APP_ID, cls._SITES_SELECTED_ROLE_GRAPH),
                (cls._SPO_RESOURCE_APP_ID, cls._SITES_SELECTED_ROLE_SPO),
            ):
                sp_resp = await client.get(
                    f"https://graph.microsoft.com/v1.0/servicePrincipals(appId='{resource_app_id}')?$select=id",
                    headers=headers,
                )
                sp_resp.raise_for_status()
                resource_id = sp_resp.json()["id"]
                assign_resp = await client.post(
                    f"https://graph.microsoft.com/v1.0/servicePrincipals/{principal_id}/appRoleAssignments",
                    headers=headers,
                    json={"principalId": principal_id, "resourceId": resource_id, "appRoleId": role_id},
                )
                if assign_resp.status_code >= 400 and "already exist" not in assign_resp.text.lower():
                    assign_resp.raise_for_status()
        log.info("Granted Sites.Selected (Graph + SharePoint Online) to %s", principal_id)

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
            # Re-granting the same principal the same role on re-provision is
            # the normal idempotent case, not an error -- Fabric 400s with a
            # message naming the conflict rather than silently no-op'ing.
            if resp.status_code >= 400 and "already" not in resp.text.lower():
                resp.raise_for_status()
        log.info("Fabric workspace %s: granted %s role to %s %s", workspace_id, role, principal_type, principal_id)

    # Microsoft Graph Command Line Tools — a Microsoft first-party client ID
    # (used by Connect-MgGraph) that is pre-authorized for broad delegated
    # Graph scopes including Sites.FullControl.All. Deliberately NOT Azure
    # CLI's own client ID (04b07795-8ddb-461a-bbee-02f9e1bf7b46) — requesting
    # Sites.FullControl.All against that one fails with AADSTS65002 ("must be
    # configured via preauthorization"), confirmed live. Graph Explorer's app
    # also works but has no scriptable API; this client ID gives the same
    # pre-authorization via a plain device-code flow we can drive ourselves.
    _GRAPH_CLI_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"

    @classmethod
    async def acquire_delegated_graph_token(
        cls, tenant_id: str, scope: str = "https://graph.microsoft.com/Sites.FullControl.All",
        poll_timeout_s: int = 600,
    ) -> str:
        """Device-code flow for one-off delegated Graph calls that need a scope
        the signed-in operator's own session doesn't have (e.g. granting
        site-level SharePoint permissions — see fabric/setup.py's "SharePoint
        Connection Prerequisites" for why this exists). Prints a URL + code for
        the operator to complete in a browser, then polls until they do."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/devicecode",
                data={"client_id": cls._GRAPH_CLI_CLIENT_ID, "scope": scope},
            )
            resp.raise_for_status()
            device = resp.json()
            print(f"\n  To authorize, open {device['verification_uri']} and enter code: {device['user_code']}\n", flush=True)

            interval = device.get("interval", 5)
            elapsed = 0
            while elapsed < poll_timeout_s:
                await asyncio.sleep(interval)
                elapsed += interval
                token_resp = await client.post(
                    f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
                    data={
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                        "client_id": cls._GRAPH_CLI_CLIENT_ID,
                        "device_code": device["device_code"],
                    },
                )
                body = token_resp.json()
                if "access_token" in body:
                    return body["access_token"]
                if body.get("error") not in ("authorization_pending", "slow_down"):
                    raise RuntimeError(f"Device code authentication failed: {body}")
        raise RuntimeError(f"Device code authentication timed out after {poll_timeout_s}s")

    @classmethod
    async def grant_site_permission(
        cls, site_id: str, app_client_id: str, app_display_name: str, tenant_id: str,
    ) -> dict:
        """Grant an Entra app read access to a specific SharePoint site via
        Graph's /sites/{id}/permissions — the mandatory site-level step for
        Sites.Selected (tenant-wide Sites.Read.All/ReadWrite.All do not
        satisfy the Fabric SharePoint connector, confirmed live). Requires a
        delegated token with Sites.FullControl.All, acquired via
        acquire_delegated_graph_token since neither the operator's own az CLI
        session nor an app-only token typically has this."""
        token = await cls.acquire_delegated_graph_token(tenant_id)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"https://graph.microsoft.com/v1.0/sites/{site_id}/permissions",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={
                    "roles": ["read"],
                    "grantedToIdentities": [{"application": {"id": app_client_id, "displayName": app_display_name}}],
                },
            )
            resp.raise_for_status()
        log.info("Granted site %s read access to app %s", site_id, app_client_id)
        return resp.json()

    @classmethod
    async def create_sharepoint_connection(
        cls, display_name: str, sharepoint_site_url: str, credential: Optional[DefaultAzureCredential] = None,
    ) -> str:
        """Create a Fabric Connection to a SharePoint site, for use as a Copy
        Job/pipeline source. Uses WorkspaceIdentity credentials — NOT
        ServicePrincipal, which Microsoft's own docs confirm is unsupported
        for Copy (only Dataflow Gen2): https://learn.microsoft.com/fabric/
        data-factory/connector-sharepoint-online-list ("Service Principal:
        n/a" for Copy). Confirmed live: a standalone App Registration with
        ServicePrincipal credentials and fully correct Sites.Selected + site
        permissions still 400s with IncorrectCredentials; WorkspaceIdentity
        with the identical permissions succeeds. Requires the *workspace's*
        identity (see grant_site_permission) to already have that site grant,
        not a separate app.
        """
        cred = credential or DefaultAzureCredential()
        token = get_bearer_token_provider(cred, FABRIC_SCOPE)()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{FABRIC_BASE}/connections",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={
                    "connectivityType": "ShareableCloud",
                    "displayName": display_name,
                    "connectionDetails": {
                        "type": "SharePoint",
                        "creationMethod": "SharePointList",
                        "parameters": [{"dataType": "Text", "name": "sharePointSiteUrl", "value": sharepoint_site_url}],
                    },
                    "privacyLevel": "Organizational",
                    "credentialDetails": {
                        "singleSignOnType": "None",
                        "connectionEncryption": "Encrypted",
                        "skipTestConnection": False,
                        "credentials": {"credentialType": "WorkspaceIdentity"},
                    },
                },
            )
            resp.raise_for_status()
        connection_id = resp.json()["id"]
        log.info("Fabric SharePoint connection created: %s (%s)", display_name, connection_id)
        return connection_id

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

    async def write_draft_to_lakehouse(
        self, draft_id: str, rfp_id: str, content_md: str, version: Optional[int] = None
    ) -> str:
        """Write a generated RFP draft to OneLake for archival and Power BI lineage.

        `version` distinguishes edits of the same draft_id — without it, every
        edit would silently overwrite the previous file at the same path since
        the filename is otherwise static (no timestamp, unlike write_eval_record).
        Original generation omits it; each subsequent edit passes an incrementing
        version so the full history accumulates as distinct files."""
        suffix = f"_v{version}" if version else ""
        file_name = f"{rfp_id}_{draft_id}{suffix}.md"
        return await self.upload_rfp_document(file_name, content_md.encode(), folder="generated-drafts")

    # ── Fabric Ingestion (Copy Job) ──────────────────────────────────────────────

    @classmethod
    async def provision_ingestion_pipeline(
        cls,
        workspace_id: str,
        pipeline_name: str,
        sharepoint_connection_id: str,
        sharepoint_folder_path: str,
        lakehouse_id: str,
        destination_folder_path: str = "rfp-corpus",
        credential: Optional[DefaultAzureCredential] = None,
    ) -> str:
        """Create a Fabric CopyJob for SharePoint → OneLake file ingestion.

        A standalone CopyJob item (Fabric's newer, simpler alternative to a
        DataPipeline wrapping an inline Copy activity) — confirmed live this
        works and runs on its own; no wrapping DataPipeline/InvokeCopyJob
        activity is needed. Uses Binary format on both source and
        destination — this is a byte-for-byte file copy (preserves .docx,
        .pdf, .md, anything, unmodified), not DelimitedText/JSON/Avro/ORC/
        Parquet, which would try to parse file contents as structured/tabular
        data and corrupt non-tabular files. Binary does not appear in the
        Fabric portal's own "File format" dropdown for this item type
        (misleadingly suggests it's unsupported) but is confirmed live via a
        real completed run + byte-for-byte diff against the source file —
        genuinely works despite the UI not listing it as an option.

        `sharepoint_connection_id` must come from create_sharepoint_connection()
        (WorkspaceIdentity credentials) — see that method's docstring for why
        ServicePrincipal doesn't work here. Runs in jobMode "CDC" (Change Data
        Capture) — an ongoing incremental sync, not a one-time copy; Fabric
        schedules follow-up runs automatically.
        """
        cred = credential or DefaultAzureCredential()
        token = get_bearer_token_provider(cred, FABRIC_SCOPE)()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        copyjob_content = {
            "properties": {
                "jobMode": "CDC",
                "source": {
                    "type": "Binary",
                    "connectionSettings": {
                        "type": "SharePointOnlineFile",
                        "externalReferences": {"connection": sharepoint_connection_id},
                    },
                },
                "destination": {
                    "type": "Binary",
                    "connectionSettings": {
                        "type": "Lakehouse",
                        "typeProperties": {
                            "workspaceId": workspace_id,
                            "artifactId": lakehouse_id,
                            "rootFolder": "Files",
                        },
                    },
                },
                "policy": {"timeout": "0.12:00:00", "retry": 0},
            },
            "activities": [
                {
                    "id": str(uuid.uuid4()),
                    "properties": {
                        "source": {
                            "datasetSettings": {
                                "location": {
                                    "type": "SharePointOnlineFileStoreLocation",
                                    "folderPath": sharepoint_folder_path,
                                }
                            },
                            "changeDataSettings": {"readMethod": "SnapshotPlusIncremental"},
                            "storeSettings": {"recursive": True},
                        },
                        "destination": {
                            "datasetSettings": {
                                "location": {"type": "LakehouseLocation", "folderPath": destination_folder_path}
                            },
                            "storeSettings": {"copyBehavior": "PreserveHierarchy"},
                        },
                        "enableStaging": False,
                    },
                }
            ],
        }
        platform_content = {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
            "metadata": {"type": "CopyJob", "displayName": pipeline_name},
            "config": {"version": "2.0", "logicalId": str(uuid.uuid4())},
        }

        item_def = {
            "displayName": pipeline_name,
            "type": "CopyJob",
            "definition": {
                "parts": [
                    {
                        "path": "copyjob-content.json",
                        "payload": base64.b64encode(json.dumps(copyjob_content).encode()).decode(),
                        "payloadType": "InlineBase64",
                    },
                    {
                        "path": ".platform",
                        "payload": base64.b64encode(json.dumps(platform_content).encode()).decode(),
                        "payloadType": "InlineBase64",
                    },
                ]
            },
        }

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(f"{FABRIC_BASE}/workspaces/{workspace_id}/items", headers=headers, json=item_def)
            resp.raise_for_status()

        pipeline_id = resp.json()["id"]
        log.info("Fabric CopyJob created: %s (%s)", pipeline_name, pipeline_id)
        return pipeline_id
