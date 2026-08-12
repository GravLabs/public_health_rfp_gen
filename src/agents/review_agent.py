"""Proposal Review Agent — scores submitted proposals against RFP evaluation criteria."""
from ._base import BaseAgent, AgentConfig

_INSTRUCTIONS = """
You are a public health grants reviewer for APHL. You evaluate submitted laboratory proposals
against the RFP's evaluation criteria and produce structured, defensible scoring.

For each criterion:
1. Extract relevant evidence from the proposal.
2. Score 0–100 with a one-sentence justification.
3. Flag any missing required elements (CLIA cert, PI named, budget justification, etc.).

Output a JSON object: { "criterion_name": { "score": int, "evidence": str, "flags": [str] } }
"""

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "retrieve_rfp",
            "description": "Retrieve the RFP document for a given RFP ID.",
            "parameters": {
                "type": "object",
                "properties": {"rfp_id": {"type": "string"}},
                "required": ["rfp_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "score_criterion",
            "description": "Score a proposal section against a named evaluation criterion.",
            "parameters": {
                "type": "object",
                "properties": {
                    "criterion": {"type": "string"},
                    "proposal_text": {"type": "string"},
                    "max_points": {"type": "integer"},
                },
                "required": ["criterion", "proposal_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract_evidence",
            "description": "Extract specific quotes from proposal text supporting a criterion.",
            "parameters": {
                "type": "object",
                "properties": {
                    "criterion": {"type": "string"},
                    "proposal_text": {"type": "string"},
                },
                "required": ["criterion", "proposal_text"],
            },
        },
    },
]


class ProposalReviewAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__(AgentConfig(
            name="proposal-review",
            instructions=_INSTRUCTIONS,
            model="gpt-4o-finetuned",
            tools=_TOOLS,
        ))
