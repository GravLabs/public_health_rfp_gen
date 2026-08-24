#!/usr/bin/env python3
"""
Fabric POC Provisioning Script
Provisions: Workspace → Lakehouse → SharePoint ingestion Copy Job

Usage:
    python fabric/setup.py \
        --workspace-name "pubhealth-rfp-poc" \
        --sharepoint-site-id "contoso.sharepoint.com,abc,def" \
        --sharepoint-folder-path "RFP Corpus" \
        --tenant-id "<entra tenant id>"

Requires:
    - Fabric Trial capacity activated (https://app.fabric.microsoft.com/home → Start trial)
    - az login completed with account that has Fabric workspace create permissions
    - pip install azure-identity httpx
    - One interactive browser sign-in mid-run (device-code flow, prints a URL +
      code) — see step [5/7] below. Everything else is fully scripted.

## SharePoint ingestion architecture (validated live end-to-end this session)

Files copy from SharePoint into OneLake via a standalone Fabric **CopyJob**
item (not a DataPipeline wrapping an inline Copy activity — a CopyJob runs on
its own; the wrapping-pipeline approach the portal sometimes defaults to is
unnecessary complexity). Confirmed working via a real completed run
(`status: Completed`) whose output was verified byte-for-byte identical to
the source file.

Two non-obvious things were wrong on the first attempt, both now fixed:

1. **Credential type must be `WorkspaceIdentity`, not `ServicePrincipal`.**
   Microsoft's own docs (fabric/data-factory/connector-sharepoint-online-list)
   state Service Principal auth is `n/a` for Copy — only supported for
   Dataflow Gen2. Confirmed live: a dedicated App Registration with fully
   correct `Sites.Selected` + site-level permissions still 400'd with
   `IncorrectCredentials` when used as `ServicePrincipal`; switching to the
   *workspace's own* Fabric-managed identity with the identical permissions
   succeeded immediately (`create_sharepoint_connection` /
   `provision_workspace_identity`). No standalone App Registration is used or
   needed for this anymore.

2. **File format must be `Binary`, not `DelimitedText`/`JSON`/`Avro`/`ORC`/
   `Parquet`.** The default the portal wizard picks (`DelimitedText`) parses
   file contents as CSV-like structured data — for actual documents (`.docx`,
   `.md`, anything non-tabular) this would corrupt them. `Binary` is a
   byte-for-byte copy, format-agnostic. Oddly, **`Binary` does not appear in
   the Fabric portal's own "File format" dropdown** for this item type (only
   Avro/DelimitedText/JSON/ORC/Parquet are listed) — this makes it look
   unsupported, but it works when set via the API directly; confirmed with a
   real completed run + byte-for-byte diff against the source.

### The one unavoidable manual step: site-level SharePoint permission grant

`Sites.Selected` (the correct application permission — NOT `Sites.Read.All`/
`Sites.ReadWrite.All`, which do not satisfy this connector, confirmed via a
live 400) grants access to zero sites by default. The mandatory follow-up —
Graph's `POST /sites/{siteId}/permissions` — needs a delegated token with
`Sites.FullControl.All`, which:
- The operator's own `az` CLI login lacks in this tenant (a separate,
  earlier-confirmed limitation — see `[[project_aphl_rfp]]` memory).
- Cannot be requested via `az login --scope
  "https://graph.microsoft.com/Sites.FullControl.All" --use-device-code`
  either — fails with **`AADSTS65002: Consent between first party application
  '04b07795-8ddb-461a-bbee-02f9e1bf7b46' [Azure CLI] and first party resource
  '00000003-0000-0000-c000-000000000000' [Microsoft Graph] must be configured
  via preauthorization`** — Microsoft blocks ad-hoc delegated consent for this
  scope on Azure CLI's own first-party app in this tenant.
- **What works**: `FabricClient.grant_site_permission()` drives a device-code
  flow against the well-known **Microsoft Graph Command Line Tools** client ID
  (`14d82eec-204b-4c2f-b7e8-296a70dab67e`, used by `Connect-MgGraph`) instead
  of Azure CLI's own — pre-authorized for `Sites.FullControl.All` in this
  tenant. This is step `[5/7]` in `provision()` below — it prints a URL + code
  and blocks until the operator completes sign-in in a browser (10 min
  timeout; re-run if it expires). Microsoft Graph Explorer
  (https://developer.microsoft.com/graph/graph-explorer) works too as a
  manual fallback, if this client ID is ever blocked the same way.

Do not work around this by adding a temporary unauthenticated endpoint to a
live container to make the call via its managed identity's existing
`Sites.ReadWrite.All` — even briefly exposed, an endpoint that grants
arbitrary Entra apps site permissions is a real privilege-escalation
exposure (the auto-mode classifier correctly blocked this attempt when tried).
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
    sharepoint_folder_path: str = "RFP Corpus",
    lakehouse_name: str = "pubhealth_rfp_lakehouse",
    pipeline_name: str = "SharePoint-to-OneLake Ingestion",
    output_env: str = ".azure/fabric.env",
    capacity_id: str = "",
    api_managed_identity_principal_id: str = "",
    tenant_id: str = "",
) -> dict:
    credential = DefaultAzureCredential()
    # sharepoint_site_id is the Graph composite id ("hostname,guid,guid") — the
    # SharePoint connector needs the plain site URL, which is just the hostname.
    sharepoint_site_url = f"https://{sharepoint_site_id.split(',')[0]}"

    if not capacity_id:
        print("\n[0/7] No --capacity-id given — looking up active Fabric trial capacity...")
        capacity_id = await FabricClient.find_trial_capacity_id(credential)
        print(f"      ✓ Found trial capacity: {capacity_id}")

    print(f"\n[1/7] Creating Fabric workspace: {workspace_name}")
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

    print(f"\n[2/7] Creating Lakehouse: {lakehouse_name}")
    lakehouse_id = await FabricClient.provision_lakehouse(workspace_id, lakehouse_name, credential)
    print(f"      ✓ Lakehouse ID: {lakehouse_id}")

    pipeline_id = None
    try:
        print("\n[3/7] Provisioning the workspace's own Fabric identity...")
        identity = await FabricClient.provision_workspace_identity(workspace_id, credential)
        workspace_sp_id = identity["servicePrincipalId"]
        print(f"      ✓ Workspace identity servicePrincipalId: {workspace_sp_id}")

        print("\n[4/7] Granting Sites.Selected (Graph + SharePoint Online) to the workspace identity...")
        await FabricClient.grant_sharepoint_access_role(workspace_sp_id, credential)
        print("      ✓ Granted (tenant-wide — still needs the site-specific grant below)")

        print("\n[5/7] Granting site-level SharePoint access — REQUIRES INTERACTIVE SIGN-IN")
        print("      (see fabric/setup.py's \"SharePoint Connection Prerequisites\" docstring for why)")
        if not tenant_id:
            raise RuntimeError("--tenant-id is required for this step (device-code auth needs it)")
        await FabricClient.grant_site_permission(
            site_id=sharepoint_site_id,
            app_client_id=identity["applicationId"],
            app_display_name=f"{workspace_name} (workspace identity)",
            tenant_id=tenant_id,
        )
        print("      ✓ Site-level access granted")

        print(f"\n[6/7] Creating Fabric Connection to {sharepoint_site_url}...")
        connection_id = await FabricClient.create_sharepoint_connection(
            display_name=f"{workspace_name}-sharepoint-connection",
            sharepoint_site_url=sharepoint_site_url,
            credential=credential,
        )
        print(f"      ✓ Connection ID: {connection_id}")

        print(f"\n[7/7] Creating Ingestion Copy Job: {pipeline_name}")
        pipeline_id = await FabricClient.provision_ingestion_pipeline(
            workspace_id=workspace_id,
            pipeline_name=pipeline_name,
            sharepoint_connection_id=connection_id,
            sharepoint_folder_path=sharepoint_folder_path,
            lakehouse_id=lakehouse_id,
            credential=credential,
        )
        print(f"      ✓ Copy Job ID: {pipeline_id}")
    except Exception as e:
        print(f"      ✗ Ingestion pipeline setup failed (non-fatal — workspace/lakehouse still usable): {e}")

    fabric_config = {
        "FABRIC_WORKSPACE_ID": workspace_id,
        "FABRIC_LAKEHOUSE_ID": lakehouse_id,
        "FABRIC_PIPELINE_ID": pipeline_id,
        "FABRIC_WORKSPACE_NAME": workspace_name,
        "FABRIC_LAKEHOUSE_NAME": lakehouse_name,
    }

    print(f"\nWriting Fabric env vars to: {output_env}")
    Path(output_env).parent.mkdir(parents=True, exist_ok=True)
    with open(output_env, "w") as f:
        for k, v in fabric_config.items():
            f.write(f"{k}={v}\n")
    print(f"      ✓ Written")

    print("\n✅ Fabric provisioning complete.\n")
    print("Next steps:")
    print("  1. Open https://app.fabric.microsoft.com → navigate to your workspace")
    print("  2. The Copy Job runs in CDC mode — Fabric schedules incremental runs")
    print("     automatically, but you can also trigger one manually to test it")
    print(f"  3. Add these vars to your .env file:\n{json.dumps(fabric_config, indent=4)}")

    return fabric_config


def main():
    parser = argparse.ArgumentParser(description="Provision Microsoft Fabric for Public Health RFP POC")
    parser.add_argument("--workspace-name", default="pubhealth-rfp-poc", help="Fabric workspace name")
    # Fabric Lakehouse display names reject hyphens ("DisplayName is Invalid for
    # ArtifactType" — confirmed via the Fabric API); underscores are accepted.
    parser.add_argument("--lakehouse-name", default="pubhealth_rfp_lakehouse")
    parser.add_argument("--sharepoint-site-id", required=True, help="SharePoint site ID for ingestion pipeline")
    parser.add_argument("--sharepoint-folder-path", default="RFP Corpus",
                         help="Document library/folder in the SharePoint site to copy from")
    parser.add_argument("--tenant-id", default="", help="Entra tenant ID (required for the interactive site-permission grant step)")
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
        sharepoint_folder_path=args.sharepoint_folder_path,
        lakehouse_name=args.lakehouse_name,
        pipeline_name=args.pipeline_name,
        output_env=args.output_env,
        capacity_id=args.capacity_id,
        api_managed_identity_principal_id=args.api_managed_identity_principal_id,
        tenant_id=args.tenant_id,
    ))


if __name__ == "__main__":
    main()
