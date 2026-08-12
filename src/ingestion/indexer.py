"""
Embeds chunks and uploads to Azure AI Search.
Uses DefaultAzureCredential (managed identity) — no keys.
"""

import os
from dataclasses import asdict
from openai import AzureOpenAI
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from azure.search.documents import SearchClient

from chunker import Chunk


SEARCH_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT")
INDEX_NAME = os.getenv("AZURE_SEARCH_INDEX", "pubhealth-rfp-index")
OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
EMBEDDING_DEPLOYMENT = os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-large")
BATCH_SIZE = 16


def _get_openai_client(credential: DefaultAzureCredential) -> AzureOpenAI:
    token_provider = get_bearer_token_provider(credential, "https://cognitiveservices.azure.com/.default")
    return AzureOpenAI(
        azure_endpoint=OPENAI_ENDPOINT,
        azure_ad_token_provider=token_provider,
        api_version="2024-06-01",
    )


def _embed_batch(texts: list[str], client: AzureOpenAI) -> list[list[float]]:
    response = client.embeddings.create(model=EMBEDDING_DEPLOYMENT, input=texts)
    return [item.embedding for item in response.data]


def index_chunks(chunks: list[Chunk], credential: DefaultAzureCredential) -> int:
    if not chunks:
        return 0

    oai_client = _get_openai_client(credential)
    search_client = SearchClient(
        endpoint=SEARCH_ENDPOINT,
        index_name=INDEX_NAME,
        credential=credential,
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
