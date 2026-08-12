#!/usr/bin/env python3
"""
Golden Dataset Annotator CLI
Interactive command-line tool for human reviewers to annotate AI-generated
RFP drafts with ground-truth quality scores and failure reasons.

Annotation output is used to:
  1. Tune evaluator score thresholds
  2. Build the ground-truth golden dataset for offline evaluation
  3. Track evaluator calibration drift over time

Usage:
    python src/golden/annotator_cli.py \
        --draft-dir data/eval-examples \
        --output-dir data/golden-annotated

Produces JSON files in output-dir ready for eval_pairs.py to build evaluation pairs.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

REQUIRED_SECTIONS = [
    "background", "funding_parameters", "eligibility", "scope_of_work",
    "reporting_requirements", "budget_requirements", "evaluation_criteria",
    "submission_instructions"
]

SECTION_DISPLAY = {
    "background": "Background and Purpose",
    "funding_parameters": "Funding Parameters",
    "eligibility": "Eligibility Criteria",
    "scope_of_work": "Scope of Work",
    "reporting_requirements": "Reporting Requirements",
    "budget_requirements": "Budget Requirements",
    "evaluation_criteria": "Evaluation Criteria",
    "submission_instructions": "Submission Instructions",
}

ANNOTATION_DIMENSIONS = [
    ("groundedness", "Is every factual claim traceable to the source RFP context? (0.0–1.0)"),
    ("completeness", "Are all required sections present with adequate detail? (0.0–1.0)"),
    ("parameter_accuracy", "Are all funding parameters (amount, period, cost share) correct? (0.0–1.0)"),
    ("compliance", "Does the draft avoid prohibited language and include required compliance citations? (0.0–1.0)"),
    ("coherence", "Is the draft internally consistent and professionally written? (0.0–1.0)"),
]


def _print_section(title: str, content: str, max_chars: int = 600) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")
    print(content[:max_chars])
    if len(content) > max_chars:
        print(f"  ... [{len(content) - max_chars} more chars]")


def _get_float(prompt: str, lo: float = 0.0, hi: float = 1.0) -> float:
    while True:
        raw = input(prompt).strip()
        try:
            v = float(raw)
            if lo <= v <= hi:
                return round(v, 2)
            print(f"  ⚠ Enter a value between {lo} and {hi}")
        except ValueError:
            print("  ⚠ Invalid number")


def _get_choice(prompt: str, choices: list[str]) -> str:
    choices_str = "/".join(choices)
    while True:
        raw = input(f"{prompt} [{choices_str}]: ").strip().upper()
        if raw in [c.upper() for c in choices]:
            return raw
        print(f"  ⚠ Enter one of: {choices_str}")


def annotate_draft(draft_data: dict, annotator_id: str) -> dict:
    """Run interactive annotation session for a single draft. Returns annotation dict."""
    draft = draft_data.get("generated_draft", {})
    rfp_id = draft_data.get("rfp_id", "UNKNOWN")
    program_area = draft_data.get("program_area", "")
    existing_scores = draft_data.get("evaluation_scores", {})

    print(f"\n{'═' * 60}")
    print(f"  ANNOTATING: {rfp_id}")
    print(f"  Program Area: {program_area}")
    print(f"  Existing model scores: {json.dumps(existing_scores, indent=2)}")
    print(f"{'═' * 60}")

    # Show draft sections for review
    for section_key in REQUIRED_SECTIONS:
        content = draft.get(section_key, "")
        if content:
            _print_section(SECTION_DISPLAY[section_key], content)
        else:
            print(f"\n  ⚠ MISSING: {SECTION_DISPLAY[section_key]}")

    print(f"\n{'─' * 60}")
    print("  ANNOTATION SCORES")
    print("  Rate each dimension 0.0 (worst) to 1.0 (best).")
    print("  Press ENTER to accept model score in [brackets].")
    print(f"{'─' * 60}\n")

    human_scores: dict[str, float] = {}
    for dimension, description in ANNOTATION_DIMENSIONS:
        model_score = existing_scores.get(dimension, None)
        hint = f" [model: {model_score:.2f}]" if model_score is not None else ""
        score = _get_float(f"  {description}{hint}\n  → ", 0.0, 1.0)
        human_scores[dimension] = score

    print("\n  GATE DECISION")
    gate = _get_choice("  Human verdict (PASS/FAIL)", ["PASS", "FAIL"])

    print("\n  FAILURE REASONS (if FAIL — press ENTER with empty line to finish):")
    failure_reasons: list[str] = []
    if gate == "FAIL":
        while True:
            reason = input("  + ").strip()
            if not reason:
                break
            failure_reasons.append(reason)

    notes = input("\n  Free-form notes (optional, ENTER to skip): ").strip()

    return {
        "annotation_id": f"ann-{rfp_id}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
        "rfp_id": rfp_id,
        "program_area": program_area,
        "annotator_id": annotator_id,
        "annotated_at": datetime.utcnow().isoformat() + "Z",
        "human_scores": human_scores,
        "human_gate_decision": gate,
        "human_failure_reasons": failure_reasons,
        "model_scores": existing_scores,
        "model_gate_decision": draft_data.get("gate_decision", ""),
        "score_deltas": {
            dim: round(human_scores.get(dim, 0) - existing_scores.get(dim, 0), 3)
            for dim in human_scores
            if dim in existing_scores
        },
        "notes": notes,
    }


def run_annotation_session(draft_dir: Path, output_dir: Path, annotator_id: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    draft_files = sorted(draft_dir.glob("eval-*.json"))

    if not draft_files:
        print(f"No eval-*.json files found in {draft_dir}")
        sys.exit(1)

    print(f"\nFound {len(draft_files)} draft(s) to annotate.")
    print(f"Annotator: {annotator_id}")
    print(f"Output: {output_dir}\n")

    session_annotations: list[dict] = []
    for i, draft_path in enumerate(draft_files, 1):
        # Skip already annotated
        out_path = output_dir / f"annotated-{draft_path.stem}.json"
        if out_path.exists():
            print(f"[{i}/{len(draft_files)}] Skipping already annotated: {draft_path.name}")
            continue

        print(f"\n[{i}/{len(draft_files)}] Loading: {draft_path.name}")
        draft_data = json.loads(draft_path.read_text())

        skip = input("  Annotate this draft? [Y/n]: ").strip().lower()
        if skip == "n":
            continue

        annotation = annotate_draft(draft_data, annotator_id)
        out_path.write_text(json.dumps({**draft_data, "human_annotation": annotation}, indent=2))
        print(f"\n  ✓ Saved: {out_path.name}")
        session_annotations.append(annotation)

    if session_annotations:
        # Write session summary
        session_file = output_dir / f"session-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.json"
        session_file.write_text(json.dumps({
            "annotator_id": annotator_id,
            "session_date": datetime.utcnow().isoformat() + "Z",
            "annotations_count": len(session_annotations),
            "average_human_scores": {
                dim: round(sum(a["human_scores"][dim] for a in session_annotations) / len(session_annotations), 3)
                for dim in ["groundedness", "completeness", "parameter_accuracy", "compliance", "coherence"]
            },
            "pass_rate": sum(1 for a in session_annotations if a["human_gate_decision"] == "PASS") / len(session_annotations),
        }, indent=2))
        print(f"\n  ✓ Session summary: {session_file.name}")

    print(f"\n{'═' * 60}")
    print(f"  Annotation session complete — {len(session_annotations)} draft(s) annotated.")
    print(f"  Run eval_pairs.py to build evaluation pairs from annotations.")
    print(f"{'═' * 60}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Interactive golden dataset annotator for Public Health RFP drafts")
    parser.add_argument("--draft-dir", type=Path, default=Path("data/eval-examples"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/golden-annotated"))
    parser.add_argument("--annotator-id", default="reviewer-1")
    args = parser.parse_args()

    try:
        run_annotation_session(args.draft_dir, args.output_dir, args.annotator_id)
    except KeyboardInterrupt:
        print("\n\n  Annotation session interrupted — progress saved.")
        sys.exit(0)


if __name__ == "__main__":
    main()
