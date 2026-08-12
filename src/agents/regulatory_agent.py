"""Regulatory Watch Agent — monitors Federal Register for guidance changes affecting RFPs."""
from ._base import BaseAgent, AgentConfig

_INSTRUCTIONS = """
You are an APHL regulatory intelligence agent. You monitor the Federal Register and CDC guidance
for changes that affect public health laboratory RFP requirements.

Your tasks:
1. Identify new or amended regulations relevant to APHL cooperative agreements (2 CFR, 45 CFR, CDC MMWR).
2. Diff against prior guidance to surface material changes (new restrictions, new eligible costs, etc.).
3. Flag any open RFPs that may need amendment based on the new guidance.
4. Create structured alerts for the APHL grants team.

Output format for alerts:
  { "regulation": str, "effective_date": str, "change_summary": str, "affected_rfps": [str], "action_required": str }
"""

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "fetch_federal_register",
            "description": "Fetch recent Federal Register notices for a given CFR section or agency.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cfr_section": {"type": "string", "description": "e.g. '2 CFR 200'"},
                    "days_back": {"type": "integer", "default": 30},
                },
                "required": ["cfr_section"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "diff_guidance",
            "description": "Compare two versions of regulatory guidance text and return a structured diff.",
            "parameters": {
                "type": "object",
                "properties": {
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "identify_affected_rfps",
            "description": "Search the RFP corpus for documents affected by a regulatory change.",
            "parameters": {
                "type": "object",
                "properties": {"regulation_summary": {"type": "string"}},
                "required": ["regulation_summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_alert",
            "description": "Create a structured regulatory alert and store it in SharePoint.",
            "parameters": {
                "type": "object",
                "properties": {
                    "alert": {"type": "object"},
                    "notify_emails": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["alert"],
            },
        },
    },
]


class RegulatoryWatchAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__(AgentConfig(
            name="regulatory-watch",
            instructions=_INSTRUCTIONS,
            model="gpt-4o",
            tools=_TOOLS,
        ))
