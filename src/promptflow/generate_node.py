"""Prompt Flow node: generate all 8 RFP sections."""
from promptflow import tool
from openai import AzureOpenAI
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
import os

SECTIONS = [
    "background",
    "funding_parameters",
    "eligibility",
    "scope_of_work",
    "reporting_requirements",
    "budget_requirements",
    "evaluation_criteria",
    "submission_instructions",
]

SYSTEM_PROMPT = (
    "You are an expert public health grants writer for the Association of Public Health "
    "Laboratories (APHL). Write precise, federally compliant RFP sections grounded only "
    "in the provided corpus excerpts. Cite 2 CFR Part 200 where applicable."
)


def _grounding_text(chunks: list[dict], section: str) -> str:
    relevant = [c for c in chunks if c.get("section_type") == section] or chunks[:3]
    return "\n\n---\n\n".join(c["content"] for c in relevant[:3])


@tool
def generate_node(
    deployment_name: str,
    program_area: str,
    description: str,
    params: dict,
    chunks: list[dict],
) -> dict:
    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
    )
    client = AzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        azure_ad_token_provider=token_provider,
        api_version="2024-08-01-preview",
    )

    params_summary = ", ".join(f"{k}: {v}" for k, v in params.items()) if params else "not specified"
    draft = {}

    for section in SECTIONS:
        grounding = _grounding_text(chunks, section)
        user_msg = (
            f"Program area: {program_area}\n"
            f"Description: {description}\n"
            f"Funding parameters: {params_summary}\n\n"
            f"Corpus excerpts for {section}:\n{grounding}\n\n"
            f"Write the '{section.replace('_', ' ').title()}' section."
        )
        resp = client.chat.completions.create(
            model=deployment_name,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
            max_tokens=1024,
        )
        draft[section] = resp.choices[0].message.content

    return {"draft": draft}
