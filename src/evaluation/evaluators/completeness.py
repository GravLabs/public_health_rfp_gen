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
        label = section.replace("_", " ").title()
        if not content:
            missing.append(label)
            continue
        word_count = len(content.split())
        min_words = MIN_SECTION_WORDS.get(section, 20)
        if word_count < min_words:
            word = "word" if word_count == 1 else "words"
            thin.append(f"{label} is too short ({word_count} {word}, needs at least {min_words})")

    if missing or thin:
        issues = []
        if missing:
            word = "section" if len(missing) == 1 else "sections"
            issues.append(f"Missing {word}: {', '.join(missing)}")
        issues.extend(thin)
        total_issues = len(missing) + len(thin)
        score = max(0.0, 1.0 - (total_issues / len(required_sections)))
        return score, "; ".join(issues)

    return 1.0, "all sections present and substantive"
