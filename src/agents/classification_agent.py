"""Classification Agent — maps free-text descriptions to public health program area taxonomy."""
from ._base import BaseAgent, AgentConfig

_INSTRUCTIONS = """
You are an program area classifier. Given any description of a public health cooperative
agreement, map it to exactly one entry in the program area taxonomy.

Taxonomy:
  Influenza Surveillance | Whole Genome Sequencing | Antimicrobial Resistance |
  Food Safety | Emergency Preparedness | HIV/STI Testing | Tuberculosis |
  COVID-19 / Respiratory Pathogens | Bioterrorism / LRN | General Surveillance

Respond with a JSON object:
  { "program_area": "<exact taxonomy label>", "confidence": 0.0–1.0, "rationale": "<one sentence>" }

If you are unsure, pick the closest match and set confidence < 0.7.
"""

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "classify_program_area",
            "description": "Classify text into one of the program area taxonomy entries.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_taxonomy",
            "description": "Return the full program area taxonomy with descriptions.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "suggest_corrections",
            "description": "Suggest corrections when user-supplied program area doesn't match taxonomy.",
            "parameters": {
                "type": "object",
                "properties": {"user_input": {"type": "string"}},
                "required": ["user_input"],
            },
        },
    },
]


class ClassificationAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__(AgentConfig(
            name="classification",
            instructions=_INSTRUCTIONS,
            model="gpt-4o",
            tools=_TOOLS,
        ))
