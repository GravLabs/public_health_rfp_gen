"""
Compliance evaluator: checks for prohibited language and required regulatory phrases.
Deterministic regex — no LLM call, runs first in the gate pipeline.
"""

import re


def score_compliance(
    draft: dict[str, str],
    prohibited_patterns: list[str],
    required_phrases: list[str],
    required_min: int,
) -> tuple[float, str]:
    full_text = "\n\n".join(draft.values())
    issues = []

    # Check prohibited patterns — any match is a hard failure
    for pattern in prohibited_patterns:
        m = re.search(pattern, full_text)
        if m:
            issues.append(f"PROHIBITED: '{m.group(0)[:60]}...'")

    if issues:
        return 0.0, "; ".join(issues)

    # Check required compliance phrases
    found = [p for p in required_phrases if re.search(re.escape(p), full_text, re.IGNORECASE)]
    coverage = len(found) / len(required_phrases)
    missing = [p for p in required_phrases if p not in found]

    if len(found) < required_min:
        detail = f"Only {len(found)}/{len(required_phrases)} required phrases found. Missing: {missing[:5]}"
        return coverage, detail

    return coverage, f"{len(found)}/{len(required_phrases)} required phrases present"
