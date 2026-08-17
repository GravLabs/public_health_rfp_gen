"""
Main ingestion pipeline: ADLS Gen2 → Document Intelligence → Chunker → AI Search
Usage: python pipeline.py [--file path/to/rfp.md] [--all]
"""

import argparse
import os
import sys
from pathlib import Path

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

from document_parser import parse_document
from chunker import chunk_document
from indexer import index_chunks


STORAGE_ACCOUNT = os.getenv("AZURE_STORAGE_ACCOUNT")
CORPUS_CONTAINER = "rfp-corpus"


def process_blob(blob_name: str, content: bytes, credential) -> int:
    print(f"Processing: {blob_name}")
    parsed = parse_document(content, blob_name, credential)
    chunks = chunk_document(parsed, blob_name)
    indexed = index_chunks(chunks, credential)
    print(f"  → {indexed} chunks indexed from {blob_name}")
    return indexed


def run_pipeline(file_filter: str | None = None) -> None:
    # During post-provision the CLI user may lack Storage RBAC; fall back to key auth.
    storage_key = os.getenv("AZURE_STORAGE_KEY")
    credential = storage_key if storage_key else DefaultAzureCredential()
    blob_client = BlobServiceClient(
        account_url=f"https://{STORAGE_ACCOUNT}.blob.core.windows.net",
        credential=credential,
    )
    container = blob_client.get_container_client(CORPUS_CONTAINER)

    total = 0
    for blob in container.list_blobs(name_starts_with=file_filter or ""):
        if not (blob.name.endswith(".md") or blob.name.endswith(".pdf") or blob.name.endswith(".docx")):
            continue
        data = container.download_blob(blob.name).readall()
        total += process_blob(blob.name, data, credential)

    print(f"\nIngestion complete: {total} total chunks indexed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Public Health RFP ingestion pipeline")
    parser.add_argument("--file", help="Process a single blob by name prefix")
    args = parser.parse_args()
    run_pipeline(file_filter=args.file)
