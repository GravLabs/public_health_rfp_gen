"""
Prepare fine-tuning JSONL from the sample-rfps corpus.

Reads data/sample-rfps/*.md, splits each file into sections by `## N. Heading`
markers, and emits supervised chat completion triplets (system + user + assistant).
Writes data/training/train.jsonl (80%) and data/training/val.jsonl (20%).

Usage:
    python src/training/prepare_finetune_data.py
    python src/training/prepare_finetune_data.py --corpus data/sample-rfps --out data/training
"""

import argparse
import json
import random
import re
from pathlib import Path

SYSTEM_PROMPT = (
    "You are an expert public health grants writer working for the Association of "
    "Public Health Laboratories (APHL). You write precise, compliant RFP sections "
    "grounded in federal grant requirements (2 CFR Part 200), CLIA standards, and "
    "APHL member laboratory eligibility criteria."
)

SECTION_MAP = {
    "background": "background",
    "purpose": "background",
    "funding": "funding_parameters",
    "eligibility": "eligibility",
    "scope": "scope_of_work",
    "priority": "scope_of_work",
    "reporting": "reporting_requirements",
    "budget": "budget_requirements",
    "evaluation": "evaluation_criteria",
    "review": "evaluation_criteria",
    "submission": "submission_instructions",
    "application": "submission_instructions",
}

SECTION_PROMPTS = {
    "background": "Write the Background and Purpose section for a public health RFP.",
    "funding_parameters": "Write the Funding Parameters section for a public health RFP.",
    "eligibility": "Write the Eligibility Criteria section for a public health RFP.",
    "scope_of_work": "Write the Scope of Work / Priority Areas section for a public health RFP.",
    "reporting_requirements": "Write the Reporting Requirements section for a public health RFP.",
    "budget_requirements": "Write the Budget Requirements section for a public health RFP.",
    "evaluation_criteria": "Write the Evaluation Criteria section for a public health RFP.",
    "submission_instructions": "Write the Submission Instructions section for a public health RFP.",
}

_HEADING_RE = re.compile(r'^#{1,3}\s+(?:\d+\.?\s+)?(.+)', re.MULTILINE)


def _classify_section(heading: str) -> str | None:
    h = heading.lower()
    for keyword, section_type in SECTION_MAP.items():
        if keyword in h:
            return section_type
    return None


def parse_sections(text: str) -> list[tuple[str, str]]:
    """Return list of (section_type, content) from a markdown RFP file."""
    splits = list(_HEADING_RE.finditer(text))
    sections = []
    for i, match in enumerate(splits):
        heading = match.group(1).strip()
        start = match.end()
        end = splits[i + 1].start() if i + 1 < len(splits) else len(text)
        content = text[start:end].strip()
        section_type = _classify_section(heading)
        if section_type and len(content) > 100:
            sections.append((section_type, content))
    return sections


def extract_rfp_context(text: str) -> str:
    """Pull header metadata (program area, sponsor, RFP ID) as context for the user prompt."""
    lines = []
    for line in text.splitlines()[:20]:
        if any(k in line for k in ["Program Area", "RFP ID", "Federal Sponsor", "Issue Date", "Period"]):
            lines.append(line.strip().lstrip("*").rstrip("*").strip())
    return " | ".join(lines) if lines else ""


def build_triplets(corpus_dir: Path) -> list[dict]:
    triplets = []
    for md_file in sorted(corpus_dir.glob("*.md")):
        text = md_file.read_text(encoding="utf-8")
        context = extract_rfp_context(text)
        sections = parse_sections(text)
        for section_type, content in sections:
            base_prompt = SECTION_PROMPTS.get(section_type, f"Write the {section_type} section.")
            user_msg = f"{base_prompt}"
            if context:
                user_msg += f"\n\nRFP context: {context}"
            triplets.append({
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                    {"role": "assistant", "content": content},
                ]
            })
    return triplets


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", default="data/sample-rfps", type=Path)
    parser.add_argument("--out", default="data/training", type=Path)
    parser.add_argument("--seed", default=42, type=int)
    args = parser.parse_args()

    triplets = build_triplets(args.corpus)
    if not triplets:
        raise SystemExit(f"No triplets extracted from {args.corpus} — check path and file format.")

    random.seed(args.seed)
    random.shuffle(triplets)

    split = int(len(triplets) * 0.8)
    train, val = triplets[:split], triplets[split:]

    write_jsonl(args.out / "train.jsonl", train)
    write_jsonl(args.out / "val.jsonl", val)

    print(f"Extracted {len(triplets)} triplets from {args.corpus}")
    print(f"  train: {len(train)}  →  {args.out}/train.jsonl")
    print(f"  val:   {len(val)}   →  {args.out}/val.jsonl")


if __name__ == "__main__":
    main()
