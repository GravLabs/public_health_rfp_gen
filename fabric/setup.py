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
    lakehouse_name: str = "pubhealth-rfp-lakehouse",
    skill_name: str = "Public Health RFP Knowledge Base",
    pipeline_name: str = "SharePoint-to-OneLake Ingestion",
    output_env: str = ".azure/fabric.env",
) -> dict:
    credential = DefaultAzureCredential()
    print(f"\n[1/5] Creating Fabric workspace: {workspace_name}")
    workspace_id = await FabricClient.provision_workspace(workspace_name, credential)
    print(f"      ✓ Workspace ID: {workspace_id}")

    print(f"\n[2/5] Creating Lakehouse: {lakehouse_name}")
    lakehouse_id = await FabricClient.provision_lakehouse(workspace_id, lakehouse_name, credential)
    print(f"      ✓ Lakehouse ID: {lakehouse_id}")

    print(f"\n[3/5] Creating AI Skill: {skill_name}")
    skill_id = await FabricClient.provision_ai_skill(
        workspace_id=workspace_id,
        skill_name=skill_name,
        ai_search_endpoint=ai_search_endpoint,
        ai_search_index=ai_search_index,
        credential=credential,
    )
    print(f"      ✓ AI Skill ID: {skill_id}")

    print(f"\n[4/5] Creating Ingestion Pipeline: {pipeline_name}")
    pipeline_id = await FabricClient.provision_ingestion_pipeline(
        workspace_id=workspace_id,
        pipeline_name=pipeline_name,
        sharepoint_site_id=sharepoint_site_id,
        lakehouse_id=lakehouse_id,
        credential=credential,
    )
    print(f"      ✓ Pipeline ID: {pipeline_id}")

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
    parser.add_argument("--lakehouse-name", default="pubhealth-rfp-lakehouse")
    parser.add_argument("--ai-search-endpoint", required=True, help="Azure AI Search endpoint URL")
    parser.add_argument("--ai-search-index", default="pubhealth-rfp-index")
    parser.add_argument("--sharepoint-site-id", required=True, help="SharePoint site ID for ingestion pipeline")
    parser.add_argument("--skill-name", default="Public Health RFP Knowledge Base")
    parser.add_argument("--pipeline-name", default="SharePoint-to-OneLake Ingestion")
    parser.add_argument("--output-env", default=".azure/fabric.env")
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
    ))


if __name__ == "__main__":
    main()
