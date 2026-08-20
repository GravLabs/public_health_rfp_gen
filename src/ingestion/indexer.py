"""
Embeds chunks and uploads to Azure AI Search.
Uses DefaultAzureCredential (managed identity) — no keys.
"""

import os
import time
from dataclasses import asdict
from openai import AzureOpenAI, NotFoundError
from azure.core.credentials import AzureKeyCredential
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from azure.search.documents import SearchClient

from chunker import Chunk


SEARCH_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT")
INDEX_NAME = os.getenv("AZURE_SEARCH_INDEX", "pubhealth-rfp-index")
OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
EMBEDDING_DEPLOYMENT = os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-large")
BATCH_SIZE = 16


def _get_openai_client(credential: DefaultAzureCredential) -> AzureOpenAI:
    api_key = os.getenv("AZURE_OPENAI_API_KEY")
    if api_key:
        return AzureOpenAI(azure_endpoint=OPENAI_ENDPOINT, api_key=api_key, api_version="2024-06-01")
    token_provider = get_bearer_token_provider(credential, "https://cognitiveservices.azure.com/.default")
    return AzureOpenAI(
        azure_endpoint=OPENAI_ENDPOINT,
        azure_ad_token_provider=token_provider,
        api_version="2024-06-01",
    )


def _embed_batch(texts: list[str], client: AzureOpenAI) -> list[list[float]]:
    # A model deployment can report "Succeeded" at the ARM level several
    # minutes before it's actually routable — Azure's own error message says
    # "wait a moment and try again", so do exactly that instead of failing
    # the whole ingestion run over a fresh deployment's propagation delay.
    max_attempts = 6
    for attempt in range(1, max_attempts + 1):
        try:
            response = client.embeddings.create(model=EMBEDDING_DEPLOYMENT, input=texts)
            return [item.embedding for item in response.data]
        except NotFoundError:
            if attempt == max_attempts:
                raise
            print(f"      · Deployment '{EMBEDDING_DEPLOYMENT}' not yet routable "
                  f"(attempt {attempt}/{max_attempts}) — waiting 20s...")
            time.sleep(20)


def index_chunks(chunks: list[Chunk], credential: DefaultAzureCredential) -> int:
    if not chunks:
        return 0

    oai_client = _get_openai_client(credential)
    _search_key = os.getenv("AZURE_SEARCH_ADMIN_KEY")
    search_credential = AzureKeyCredential(_search_key) if _search_key else credential
    search_client = SearchClient(
        endpoint=SEARCH_ENDPOINT,
        index_name=INDEX_NAME,
        credential=search_credential,
    )

    indexed = 0
    for i in range(0, len(chunks), BATCH_SIZE):
        batch = chunks[i : i + BATCH_SIZE]
        texts = [f"{c.context_summary}\n\n{c.content}" for c in batch]
        embeddings = _embed_batch(texts, oai_client)

        docs = []
        for chunk, embedding in zip(batch, embeddings):
            doc = asdict(chunk)
            doc["content_vector"] = embedding
            docs.append(doc)

        results = search_client.upload_documents(docs)
        indexed += sum(1 for r in results if r.succeeded)

    return indexed
