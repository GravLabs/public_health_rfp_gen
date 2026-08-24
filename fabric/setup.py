#!/usr/bin/env python3
"""
Fabric POC Provisioning Script
Provisions: Workspace → Lakehouse → Ingestion Pipeline

Usage:
    python fabric/setup.py \
        --workspace-name "pubhealth-rfp-poc" \
        --sharepoint-site-id "contoso.sharepoint.com,abc,def" \
        --pipeline-app-client-id "<see SharePoint Connection Prerequisites below>" \
        --pipeline-app-secret "<...>" \
        --pipeline-app-tenant-id "<...>"

Requires:
    - Fabric Trial capacity activated (https://app.fabric.microsoft.com/home → Start trial)
    - az login completed with account that has Fabric workspace create permissions
    - pip install azure-identity httpx
    - SharePoint Connection Prerequisites completed once (see below) — the
      ingestion pipeline's Fabric Connection needs an Entra app with site-level
      SharePoint access; this cannot be fully scripted (see the AADSTS65002 note).

## SharePoint Connection Prerequisites (one-time, manual)

The ingestion pipeline copies files from SharePoint into OneLake via a Fabric
`SharePoint` connector (creationMethod `SharePointList`), authenticated as a
dedicated Entra app using `ServicePrincipal` credentials — NOT the API
container's managed identity, NOT the Teams bot's App Registration. Keeping
this identity separate and single-purpose avoids entangling unrelated
concerns (bot auth, container Graph access, pipeline data access).

Confirmed empirically this session — the Fabric SharePoint connector's
`ServicePrincipal` auth needs ALL of the following, not just an Entra app
with a secret:

1. **Create a dedicated App Registration** (this session's instance:
   `pubhealth-fabric-pipeline`, client ID `55deefbe-ac20-4f1e-a96a-12c8c51d0329`)
   with a client secret. Standard `az ad app create` + `az ad sp create` +
   `az ad app credential reset` — same pattern as the bot's own identity setup.

2. **Grant `Sites.Selected`** (NOT `Sites.Read.All` — confirmed via a live 400
   `IncorrectCredentials`/`AccessUnauthorized` test that `Sites.Read.All` alone
   is insufficient) as an **application permission**, on BOTH:
   - Microsoft Graph (`00000003-0000-0000-c000-000000000000`), app role
     `883ea226-0bf2-4a8f-9f9d-92c9162a727d`
   - SharePoint Online (`00000003-0000-0ff1-ce00-000000000000`), app role
     `d13f72ca-a275-4b96-b789-48ebcc4da984`
   `Sites.Selected` grants access to zero sites by default — step 3 is what
   actually authorizes a specific site, and is mandatory; the tenant-wide
   `Sites.Read.All`/`Sites.ReadWrite.All` roles (used elsewhere in this repo
   for the API container's managed identity) do NOT satisfy this connector.

3. **Grant the app site-level access** via Graph's
   `POST /sites/{siteId}/permissions`:
   ```json
   {
     "roles": ["read"],
     "grantedToIdentities": [{
       "application": {"id": "<pipeline app client ID>", "displayName": "pubhealth-fabric-pipeline"}
     }]
   }
   ```
   **This call cannot be made via `az rest`/Azure CLI in this tenant.** Two
   independent failures confirmed this empirically:
   - The operator's own `az` CLI login lacks `Sites.*` delegated Graph scope
     here (a separate, earlier-confirmed limitation — see
     `[[project_aphl_rfp]]` memory).
   - Requesting the scope interactively via `az login --scope
     "https://graph.microsoft.com/Sites.FullControl.All" --use-device-code`
     fails with **`AADSTS65002: Consent between first party application
     '04b07795-8ddb-461a-bbee-02f9e1bf7b46' [Azure CLI] and first party
     resource '00000003-0000-0000-c000-000000000000' [Microsoft Graph] must
     be configured via preauthorization`** — Microsoft blocks ad-hoc
     delegated consent for this scope on Azure CLI's own first-party app in
     this tenant; there is no interactive-login workaround via Azure CLI's
     own client ID.
   - A container's own managed identity (already holding
     `Sites.ReadWrite.All` app permission, used successfully all session for
     SharePoint document uploads) COULD make this call — but only from
     inside the container via IMDS. Do not add a temporary unauthenticated
     endpoint to expose this — even briefly live on a public container, an
     endpoint that grants arbitrary Entra apps site permissions is a real
     privilege-escalation exposure.
   - **What actually works, scripted**: `FabricClient.grant_site_permission()`
     drives a device-code flow against the well-known **Microsoft Graph
     Command Line Tools** client ID (`14d82eec-204b-4c2f-b7e8-296a70dab67e`,
     used by `Connect-MgGraph`) instead of Azure CLI's own — that one IS
     pre-authorized for `Sites.FullControl.All` in this tenant. Prints a URL +
     code; the operator completes sign-in in a browser, same UX as `az login
     --use-device-code`, no manual Graph Explorer request-building needed.
     (Microsoft Graph Explorer, https://developer.microsoft.com/graph/graph-explorer,
     works too as a manual fallback if this client ID is ever blocked the
     same way — its app is also pre-authorized.)

4. Only after step 3 succeeds will `FabricClient.create_connection()` (using
   `ServicePrincipal` credentials with this app's client ID/secret/tenant ID)
   stop returning `IncorrectCredentials`.

Teardown note: `scripts/teardown.sh` deletes this App Registration (tracked
as `FABRIC_PIPELINE_APP_ID` in azd env) but does not also revoke the site
permission grant — that would need re-running the device-code flow just for
cleanup. Once the app itself is deleted, the grant is inert regardless.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from azure.identity import DefaultAzureCredential

sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "api"))
from fabric_client import FabricClient


async def provision(
    workspace_name: str,
    sharepoint_site_id: str,
    lakehouse_name: str = "pubhealth_rfp_lakehouse",
    pipeline_name: str = "SharePoint-to-OneLake Ingestion",
    output_env: str = ".azure/fabric.env",
    capacity_id: str = "",
    api_managed_identity_principal_id: str = "",
) -> dict:
    credential = DefaultAzureCredential()

    if not capacity_id:
        print("\n[0/4] No --capacity-id given — looking up active Fabric trial capacity...")
        capacity_id = await FabricClient.find_trial_capacity_id(credential)
        print(f"      ✓ Found trial capacity: {capacity_id}")

    print(f"\n[1/4] Creating Fabric workspace: {workspace_name}")
    workspace_id = await FabricClient.provision_workspace(workspace_name, credential, capacity_id=capacity_id)
    print(f"      ✓ Workspace ID: {workspace_id}")

    if api_managed_identity_principal_id:
        print(f"      Granting Contributor role to API managed identity {api_managed_identity_principal_id}...")
        await FabricClient.grant_workspace_role(
            workspace_id, api_managed_identity_principal_id, role="Contributor", credential=credential
        )
        print("      ✓ Role granted (this is what write_draft_to_lakehouse/write_eval_record need at runtime)")
    else:
        print("      ⚠ No --api-managed-identity-principal-id given — the API container's writes")
        print("        (write_draft_to_lakehouse, write_eval_record) will 403 at OneLake until a")
        print("        workspace role is granted. Resolve it with:")
        print("          az identity list -g <resource-group> --query \"[0].principalId\" -o tsv")
        print("        then either re-run with --api-managed-identity-principal-id, or grant manually.")

    print(f"\n[2/4] Creating Lakehouse: {lakehouse_name}")
    lakehouse_id = await FabricClient.provision_lakehouse(workspace_id, lakehouse_name, credential)
    print(f"      ✓ Lakehouse ID: {lakehouse_id}")

    pipeline_id = None
    try:
        print(f"\n[3/4] Creating Ingestion Pipeline: {pipeline_name}")
        pipeline_id = await FabricClient.provision_ingestion_pipeline(
            workspace_id=workspace_id,
            pipeline_name=pipeline_name,
            sharepoint_site_id=sharepoint_site_id,
            lakehouse_id=lakehouse_id,
            credential=credential,
        )
        print(f"      ✓ Pipeline ID: {pipeline_id}")
    except Exception as e:
        print(f"      ✗ Ingestion Pipeline provisioning failed: {e}")

    fabric_config = {
        "FABRIC_WORKSPACE_ID": workspace_id,
        "FABRIC_LAKEHOUSE_ID": lakehouse_id,
        "FABRIC_PIPELINE_ID": pipeline_id,
        "FABRIC_WORKSPACE_NAME": workspace_name,
        "FABRIC_LAKEHOUSE_NAME": lakehouse_name,
    }

    print(f"\n[4/4] Writing Fabric env vars to: {output_env}")
    Path(output_env).parent.mkdir(parents=True, exist_ok=True)
    with open(output_env, "w") as f:
        for k, v in fabric_config.items():
            f.write(f"{k}={v}\n")
    print(f"      ✓ Written")

    print("\n✅ Fabric provisioning complete.\n")
    print("Next steps:")
    print("  1. Open https://app.fabric.microsoft.com → navigate to your workspace")
    print("  2. Run the ingestion pipeline to copy SharePoint documents to OneLake")
    print(f"  3. Add these vars to your .env file:\n{json.dumps(fabric_config, indent=4)}")

    return fabric_config


def main():
    parser = argparse.ArgumentParser(description="Provision Microsoft Fabric for Public Health RFP POC")
    parser.add_argument("--workspace-name", default="pubhealth-rfp-poc", help="Fabric workspace name")
    # Fabric Lakehouse display names reject hyphens ("DisplayName is Invalid for
    # ArtifactType" — confirmed via the Fabric API); underscores are accepted.
    parser.add_argument("--lakehouse-name", default="pubhealth_rfp_lakehouse")
    parser.add_argument("--sharepoint-site-id", required=True, help="SharePoint site ID for ingestion pipeline")
    parser.add_argument("--pipeline-name", default="SharePoint-to-OneLake Ingestion")
    parser.add_argument("--output-env", default=".azure/fabric.env")
    parser.add_argument("--capacity-id", default="", help="Fabric capacity ID to assign the workspace to (auto-detects the active trial capacity if omitted)")
    parser.add_argument("--api-managed-identity-principal-id", default="",
                         help="Principal ID of the API container's managed identity — granted Contributor on the "
                              "new workspace so write_draft_to_lakehouse/write_eval_record don't 403 at runtime. "
                              "Resolve with: az identity list -g <resource-group> --query \"[0].principalId\" -o tsv")
    args = parser.parse_args()

    asyncio.run(provision(
        workspace_name=args.workspace_name,
        sharepoint_site_id=args.sharepoint_site_id,
        lakehouse_name=args.lakehouse_name,
        pipeline_name=args.pipeline_name,
        output_env=args.output_env,
        capacity_id=args.capacity_id,
        api_managed_identity_principal_id=args.api_managed_identity_principal_id,
    ))


if __name__ == "__main__":
    main()
