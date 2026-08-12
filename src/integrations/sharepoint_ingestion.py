"""
SharePoint → OneLake → AI Search ingestion pipeline.
Downloads RFP corpus documents from SharePoint, stores in Fabric OneLake,
then indexes into Azure AI Search for RAG retrieval.

Can be run standalone or called from the FastAPI /sharepoint/ingest endpoint.

Usage:
    python src/integrations/sharepoint_ingestion.py \
        --site-id "contoso.sharepoint.com,abc,def" \
        --library "RFP Corpus"
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "api"))
sys.path.insert(0, str(Path(__file__).parent.parent / "ingestion"))

from azure.identity import DefaultAzureCredential
from sharepoint_client import SharePointClient
from fabric_client import FabricClient

log = logging.getLogger(__name__)


class SharePointIngestionPipeline:
    """
    End-to-end pipeline:
      SharePoint library → local temp → OneLake (Fabric) → AI Search index
    """

    def __init__(
        self,
        site_id: str,
        library_name: str = "RFP Corpus",
        fabric_workspace_id: str = "",
        fabric_lakehouse_id: str = "",
    ):
        credential = DefaultAzureCredential()
        self._sp = SharePointClient(site_id, credential)
        self._library = library_name
        self._fabric: FabricClient | None = None
        if fabric_workspace_id and fabric_lakehouse_id:
            self._fabric = FabricClient(fabric_workspace_id, fabric_lakehouse_id, credential)

    async def run(self, extensions: tuple[str, ...] = (".pdf", ".docx", ".md")) -> dict:
        """
        Execute the full ingestion pipeline.
        Returns a summary with counts and any errors.
        """
        from pipeline import run_file_ingestion  # type: ignore

        summary = {"downloaded": 0, "indexed": 0, "fabric_uploaded": 0, "errors": []}

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            log.info("Downloading corpus from SharePoint library: %s", self._library)
            try:
                downloaded = await self._sp.download_library_to_local(
                    self._library, tmp_path, extensions
                )
                summary["downloaded"] = len(downloaded)
                log.info("Downloaded %d files", len(downloaded))
            except Exception as e:
                log.error("SharePoint download failed: %s", e)
                summary["errors"].append(f"sharepoint_download: {e}")
                return summary

            for file_path in downloaded:
                # Upload to OneLake for durability
                if self._fabric:
                    try:
                        content = file_path.read_bytes()
                        await self._fabric.upload_rfp_document(file_path.name, content)
                        summary["fabric_uploaded"] += 1
                    except Exception as e:
                        log.warning("OneLake upload failed for %s: %s", file_path.name, e)
                        summary["errors"].append(f"fabric_upload:{file_path.name}: {e}")

                # Index into AI Search
                try:
                    count = await run_file_ingestion(str(file_path))
                    summary["indexed"] += count
                    log.info("Indexed %d chunks from %s", count, file_path.name)
                except Exception as e:
                    log.warning("Indexing failed for %s: %s", file_path.name, e)
                    summary["errors"].append(f"indexing:{file_path.name}: {e}")

        log.info("Ingestion complete: %s", summary)
        return summary


async def run(site_id: str, library: str, fabric_workspace_id: str = "", fabric_lakehouse_id: str = "") -> dict:
    pipeline = SharePointIngestionPipeline(
        site_id=site_id,
        library_name=library,
        fabric_workspace_id=fabric_workspace_id,
        fabric_lakehouse_id=fabric_lakehouse_id,
    )
    return await pipeline.run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description="SharePoint → OneLake → AI Search ingestion")
    parser.add_argument("--site-id", required=True)
    parser.add_argument("--library", default="RFP Corpus")
    parser.add_argument("--fabric-workspace-id", default=os.getenv("FABRIC_WORKSPACE_ID", ""))
    parser.add_argument("--fabric-lakehouse-id", default=os.getenv("FABRIC_LAKEHOUSE_ID", ""))
    args = parser.parse_args()

    result = asyncio.run(run(args.site_id, args.library, args.fabric_workspace_id, args.fabric_lakehouse_id))
    print(f"\nIngestion summary: {result}")
