"""
Creates the Azure AI Search index for Public Health RFP documents.
Uses hybrid search (keyword + vector) with semantic ranker.
Run once after azd up via post-provision hook.
"""

import os
from azure.core.credentials import AzureKeyCredential
from azure.identity import DefaultAzureCredential
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    SearchIndex, SearchField, SearchFieldDataType,
    SimpleField, SearchableField, VectorSearch,
    HnswAlgorithmConfiguration, VectorSearchProfile,
    SemanticConfiguration, SemanticSearch, SemanticPrioritizedFields,
    SemanticField,
)

INDEX_NAME = os.getenv("AZURE_SEARCH_INDEX", "pubhealth-rfp-index")
SEARCH_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT")
EMBEDDING_DIMENSIONS = 1536  # text-embedding-3-small


def create_rfp_index() -> None:
    _key = os.getenv("AZURE_SEARCH_ADMIN_KEY")
    credential = AzureKeyCredential(_key) if _key else DefaultAzureCredential()
    client = SearchIndexClient(endpoint=SEARCH_ENDPOINT, credential=credential)

    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True, filterable=True),
        SearchableField(name="content", type=SearchFieldDataType.String, analyzer_name="en.microsoft"),
        SearchableField(name="section_type", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="rfp_id", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="fiscal_year", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="program_area", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="federal_sponsor", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="source_file", type=SearchFieldDataType.String, retrievable=True),
        SimpleField(name="chunk_index", type=SearchFieldDataType.Int32, filterable=True),
        SearchableField(name="context_summary", type=SearchFieldDataType.String),  # contextual chunk prefix
        SearchField(
            name="content_vector",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=EMBEDDING_DIMENSIONS,
            vector_search_profile_name="hnsw-profile",
        ),
    ]

    vector_search = VectorSearch(
        algorithms=[HnswAlgorithmConfiguration(name="hnsw-config")],
        profiles=[VectorSearchProfile(name="hnsw-profile", algorithm_configuration_name="hnsw-config")],
    )

    semantic_config = SemanticConfiguration(
        name="pubhealth-rfp-semantic",
        prioritized_fields=SemanticPrioritizedFields(
            title_field=SemanticField(field_name="section_type"),
            content_fields=[SemanticField(field_name="content")],
            keywords_fields=[SemanticField(field_name="program_area"), SemanticField(field_name="rfp_id")],
        ),
    )

    index = SearchIndex(
        name=INDEX_NAME,
        fields=fields,
        vector_search=vector_search,
        semantic_search=SemanticSearch(configurations=[semantic_config], default_configuration_name="pubhealth-rfp-semantic"),
    )

    client.create_or_update_index(index)
    print(f"Index '{INDEX_NAME}' created/updated at {SEARCH_ENDPOINT}")


if __name__ == "__main__":
    create_rfp_index()
