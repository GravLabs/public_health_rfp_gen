"""Prompt Flow node: hybrid AI Search retrieval."""
from promptflow import tool
from azure.search.documents import SearchClient
from azure.search.documents.models import VectorizedQuery
from azure.identity import DefaultAzureCredential
from azure.core.credentials import AzureKeyCredential
import os


@tool
def search_node(program_area: str, description: str, top_k: int = 8) -> list[dict]:
    endpoint = os.environ["AZURE_SEARCH_ENDPOINT"]
    index = os.environ.get("AZURE_SEARCH_INDEX", "pubhealth-rfp-index")

    client = SearchClient(
        endpoint=endpoint,
        index_name=index,
        credential=DefaultAzureCredential(),
    )

    query_text = f"{program_area}: {description}"
    results = client.search(
        search_text=query_text,
        top=top_k,
        select=["content", "section_type", "rfp_id", "program_area"],
    )

    return [
        {
            "content": r["content"],
            "section_type": r.get("section_type", ""),
            "rfp_id": r.get("rfp_id", ""),
            "program_area": r.get("program_area", ""),
        }
        for r in results
    ]
