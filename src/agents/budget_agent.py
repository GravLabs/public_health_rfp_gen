"""Budget Audit Agent — validates RFP budget narratives for allowability and accuracy."""
from ._base import BaseAgent, AgentConfig

_INSTRUCTIONS = """
You are a public health grants budget compliance auditor. You review budget narratives in RFP submissions
to verify they meet federal cost principles (2 CFR Part 200) and award requirements.

For each budget line:
1. Check allowability: is this cost type permitted under 2 CFR 200.405?
2. Check allocability: is the cost directly attributable to the award objectives?
3. Verify arithmetic: do line totals match the stated total award amount?
4. Flag indirect cost rate mismatches against the negotiated rate on file.

Output a JSON object:
  {
    "total_verified": bool,
    "line_items": [{ "item": str, "allowable": bool, "issue": str | null }],
    "flags": [str],
    "recommendation": "approve" | "revise" | "reject"
  }
"""

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "parse_budget_narrative",
            "description": "Extract structured line items from a budget narrative document.",
            "parameters": {
                "type": "object",
                "properties": {"narrative_text": {"type": "string"}},
                "required": ["narrative_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_parameters",
            "description": "Verify that the budget total matches the RFP award parameters.",
            "parameters": {
                "type": "object",
                "properties": {
                    "budget_total": {"type": "number"},
                    "rfp_award_range": {
                        "type": "object",
                        "properties": {
                            "min": {"type": "number"},
                            "max": {"type": "number"},
                        },
                    },
                },
                "required": ["budget_total"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "flag_discrepancy",
            "description": "Record a budget discrepancy for human review.",
            "parameters": {
                "type": "object",
                "properties": {
                    "line_item": {"type": "string"},
                    "issue": {"type": "string"},
                    "severity": {"type": "string", "enum": ["info", "warning", "error"]},
                },
                "required": ["line_item", "issue"],
            },
        },
    },
]


class BudgetAuditAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__(AgentConfig(
            name="budget-audit",
            instructions=_INSTRUCTIONS,
            model="gpt-4o",
            tools=_TOOLS,
        ))
