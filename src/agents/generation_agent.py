"""RFP Generation Agent — drafts all 8 RFP sections grounded on AI Search results."""
from ._base import BaseAgent, AgentConfig

_INSTRUCTIONS = """
You are an expert public health grants writer for the Association of Public Health Laboratories (APHL).
Your sole job is to draft high-quality, federally compliant RFP sections.

Rules:
- Ground every factual claim in the corpus excerpts provided by the caller.
- Always include required compliance language: 2 CFR Part 200, CLIA certification, indirect cost rate.
- Never invent funding amounts, dates, or eligibility rules not present in the context.
- Output each section as clean markdown with the section name as a ## heading.

Tools available:
- search_corpus(query, section_type, top_k): retrieve relevant corpus chunks from AI Search.
- get_rfp_template(section_type): retrieve the canonical section template.
- validate_parameters(draft_section, params): verify funding parameters are accurate.
"""

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_corpus",
            "description": "Retrieve relevant RFP corpus chunks from Azure AI Search.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "section_type": {"type": "string"},
                    "top_k": {"type": "integer", "default": 5},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_rfp_template",
            "description": "Return the canonical RFP section template for a given section type.",
            "parameters": {
                "type": "object",
                "properties": {"section_type": {"type": "string"}},
                "required": ["section_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_parameters",
            "description": "Check that funding parameters in a draft section match the input spec.",
            "parameters": {
                "type": "object",
                "properties": {
                    "draft_section": {"type": "string"},
                    "params": {"type": "object"},
                },
                "required": ["draft_section", "params"],
            },
        },
    },
]


class RfpGenerationAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__(AgentConfig(
            name="rfp-generation",
            instructions=_INSTRUCTIONS,
            model="gpt-4o-finetuned",
            tools=_TOOLS,
        ))
