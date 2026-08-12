"""
Section completeness evaluator: verifies all required sections are present
and meet minimum content thresholds.
"""

MIN_SECTION_WORDS = {
    "background": 50,
    "funding_parameters": 20,
    "eligibility": 30,
    "scope_of_work": 80,
    "reporting_requirements": 20,
    "budget_requirements": 20,
    "evaluation_criteria": 20,
    "submission_instructions": 20,
}


def score_completeness(draft: dict[str, str], required_sections: list[str]) -> tuple[float, str]:
    missing = []
    thin = []

    for section in required_sections:
        content = draft.get(section, "").strip()
        if not content:
            missing.append(section)
            continue
        word_count = len(content.split())
        min_words = MIN_SECTION_WORDS.get(section, 20)
        if word_count < min_words:
            thin.append(f"{section}({word_count}<{min_words}w)")

    if missing or thin:
        issues = []
        if missing:
            issues.append(f"missing_sections={missing}")
        if thin:
            issues.append(f"thin_sections={thin}")
        total_issues = len(missing) + len(thin)
        score = max(0.0, 1.0 - (total_issues / len(required_sections)))
        return score, "; ".join(issues)

    return 1.0, "all sections present and substantive"
