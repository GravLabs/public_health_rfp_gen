#!/usr/bin/env python3
"""
Fabric POC Provisioning Script
Provisions: Workspace → Lakehouse → AI Skill → Ingestion Pipeline

Usage:
    python fabric/setup.py \
        --workspace-name "pubhealth-rfp-poc" \
        --ai-search-endpoint "https://srch-xxx.search.windows.net" \
        --sharepoint-site-id "contoso.sharepoint.com,abc,def"

Requires:
    - Fabric Trial capacity activated (https://app.fabric.microsoft.com/home → Start trial)
    - az login completed with account that has Fabric workspace create permissions
    - pip install azure-identity httpx
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
    ai_search_endpoint: str,
    ai_search_index: str,
    sharepoint_site_id: str,
    lakehouse_name: str = "pubhealth_rfp_lakehouse",
    skill_name: str = "Public Health RFP Knowledge Base",
    pipeline_name: str = "SharePoint-to-OneLake Ingestion",
    output_env: str = ".azure/fabric.env",
    capacity_id: str = "",
    api_managed_identity_principal_id: str = "",
) -> dict:
    credential = DefaultAzureCredential()

    if not capacity_id:
        print("\n[0/5] No --capacity-id given — looking up active Fabric trial capacity...")
        capacity_id = await FabricClient.find_trial_capacity_id(credential)
        print(f"      ✓ Found trial capacity: {capacity_id}")

    print(f"\n[1/5] Creating Fabric workspace: {workspace_name}")
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

    print(f"\n[2/5] Creating Lakehouse: {lakehouse_name}")
    lakehouse_id = await FabricClient.provision_lakehouse(workspace_id, lakehouse_name, credential)
    print(f"      ✓ Lakehouse ID: {lakehouse_id}")

    # AI Skill and pipeline provisioning use the Fabric generic-item "definition"
    # API, which has stricter shape requirements (path/payloadType/base64 payload)
    # than these calls currently send — a pre-existing bug, confirmed via a 400
    # from the live API, separate from and out of scope for the workspace/lakehouse
    # write path draft persistence actually depends on. Non-blocking: workspace +
    # lakehouse are what matters for write_draft_to_lakehouse/write_eval_record,
    # so a failure here shouldn't prevent writing out that config.
    skill_id = None
    try:
        print(f"\n[3/5] Creating AI Skill: {skill_name}")
        skill_id = await FabricClient.provision_ai_skill(
            workspace_id=workspace_id,
            skill_name=skill_name,
            ai_search_endpoint=ai_search_endpoint,
            ai_search_index=ai_search_index,
            credential=credential,
        )
        print(f"      ✓ AI Skill ID: {skill_id}")
    except Exception as e:
        print(f"      ✗ AI Skill provisioning failed (non-fatal, known issue — see comment above): {e}")

    pipeline_id = None
    try:
        print(f"\n[4/5] Creating Ingestion Pipeline: {pipeline_name}")
        pipeline_id = await FabricClient.provision_ingestion_pipeline(
            workspace_id=workspace_id,
            pipeline_name=pipeline_name,
            sharepoint_site_id=sharepoint_site_id,
            lakehouse_id=lakehouse_id,
            credential=credential,
        )
        print(f"      ✓ Pipeline ID: {pipeline_id}")
    except Exception as e:
        print(f"      ✗ Ingestion Pipeline provisioning failed (non-fatal, known issue — see comment above): {e}")

    fabric_config = {
        "FABRIC_WORKSPACE_ID": workspace_id,
        "FABRIC_LAKEHOUSE_ID": lakehouse_id,
        "FABRIC_AI_SKILL_ID": skill_id,
        "FABRIC_PIPELINE_ID": pipeline_id,
        "FABRIC_WORKSPACE_NAME": workspace_name,
        "FABRIC_LAKEHOUSE_NAME": lakehouse_name,
    }

    print(f"\n[5/5] Writing Fabric env vars to: {output_env}")
    Path(output_env).parent.mkdir(parents=True, exist_ok=True)
    with open(output_env, "w") as f:
        for k, v in fabric_config.items():
            f.write(f"{k}={v}\n")
    print(f"      ✓ Written")

    print("\n✅ Fabric provisioning complete.\n")
    print("Next steps:")
    print("  1. Open https://app.fabric.microsoft.com → navigate to your workspace")
    print("  2. Run the ingestion pipeline to copy SharePoint documents to OneLake")
    print("  3. Open the AI Skill and test queries against the RFP corpus")
    print(f"  4. Add these vars to your .env file:\n{json.dumps(fabric_config, indent=4)}")

    return fabric_config


def main():
    parser = argparse.ArgumentParser(description="Provision Microsoft Fabric for Public Health RFP POC")
    parser.add_argument("--workspace-name", default="pubhealth-rfp-poc", help="Fabric workspace name")
    # Fabric Lakehouse display names reject hyphens ("DisplayName is Invalid for
    # ArtifactType" — confirmed via the Fabric API); underscores are accepted.
    parser.add_argument("--lakehouse-name", default="pubhealth_rfp_lakehouse")
    parser.add_argument("--ai-search-endpoint", required=True, help="Azure AI Search endpoint URL")
    parser.add_argument("--ai-search-index", default="pubhealth-rfp-index")
    parser.add_argument("--sharepoint-site-id", required=True, help="SharePoint site ID for ingestion pipeline")
    parser.add_argument("--skill-name", default="Public Health RFP Knowledge Base")
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
        ai_search_endpoint=args.ai_search_endpoint,
        ai_search_index=args.ai_search_index,
        sharepoint_site_id=args.sharepoint_site_id,
        lakehouse_name=args.lakehouse_name,
        skill_name=args.skill_name,
        pipeline_name=args.pipeline_name,
        output_env=args.output_env,
        capacity_id=args.capacity_id,
        api_managed_identity_principal_id=args.api_managed_identity_principal_id,
    ))


if __name__ == "__main__":
    main()
