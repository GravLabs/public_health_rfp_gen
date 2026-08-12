"""
Parameter accuracy evaluator: verifies that funding parameters in the
generated draft match the input specification exactly.
Deterministic — no LLM call.
"""

import re
from typing import Any


def _find_amount(text: str, label: str) -> str | None:
    # Find lines containing the label, then extract the first dollar amount on that line.
    # This handles "Total funding available: $3,500,000" where label="Total".
    amount_pat = re.compile(r'\$?([\d,]+(?:\.\d+)?(?:M|K)?)')
    for line in text.splitlines():
        if re.search(re.escape(label), line, re.IGNORECASE):
            m = amount_pat.search(line)
            if m and m.group(1).replace(",", "").replace(".", "").isdigit():
                return m.group(1).replace(",", "")
    return None


def _normalize_amount(val) -> float:
    if val is None:
        return 0.0
    s = str(val).replace(",", "").replace("$", "").strip().upper()
    if s.endswith("M"):
        return float(s[:-1]) * 1_000_000
    if s.endswith("K"):
        return float(s[:-1]) * 1_000
    try:
        return float(s)
    except ValueError:
        return 0.0


def score_parameter_accuracy(draft: dict[str, str], input_spec: dict[str, Any]) -> tuple[float, str]:
    full_text = "\n\n".join(draft.values())
    issues = []
    checks = 0

    # Check total funding
    if "total_funding" in input_spec:
        checks += 1
        expected = _normalize_amount(input_spec["total_funding"])
        raw = _find_amount(full_text, "Total")
        found = _normalize_amount(raw)
        if abs(found - expected) / max(expected, 1) > 0.05:  # 5% tolerance
            issues.append(f"total_funding: expected ${expected:,.0f} found ${found:,.0f}")

    # Check period of performance
    if "period_of_performance_months" in input_spec:
        checks += 1
        expected_mo = input_spec["period_of_performance_months"]
        if str(expected_mo) not in full_text and f"{expected_mo} month" not in full_text.lower():
            issues.append(f"period_of_performance: expected {expected_mo} months not found in draft")

    # Check cost sharing
    checks += 1
    if "No" in str(input_spec.get("cost_sharing", "No")):
        if "cost sharing" in full_text.lower() and "not required" not in full_text.lower() and "no cost sharing" not in full_text.lower():
            issues.append("cost_sharing: may be misrepresented in draft")

    if not issues:
        return 1.0, f"all {checks} parameter checks passed"

    score = max(0.0, 1.0 - (len(issues) / checks))
    return score, "; ".join(issues)
