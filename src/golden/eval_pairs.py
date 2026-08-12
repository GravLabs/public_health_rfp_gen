"""
Evaluation Pair Builder
Reads human-annotated draft files from data/golden-annotated and builds
structured evaluation pairs for:
  1. Offline evaluator calibration (compare model vs human scores)
  2. Threshold tuning (find optimal gate decision thresholds per dimension)
  3. Confusion matrix construction (TP/TN/FP/FN for gate decisions)

Outputs:
  data/golden-dataset/eval_pairs.jsonl  — one line per annotated draft
  data/golden-dataset/calibration_report.json  — evaluator bias and MAE

Usage:
    python src/golden/eval_pairs.py \
        --annotated-dir data/golden-annotated \
        --output-dir data/golden-dataset
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


DIMENSIONS = ["groundedness", "completeness", "parameter_accuracy", "compliance", "coherence"]


def _load_annotated(annotated_dir: Path) -> list[dict]:
    files = sorted(annotated_dir.glob("annotated-*.json"))
    items = []
    for f in files:
        data = json.loads(f.read_text())
        if "human_annotation" in data:
            items.append(data)
    return items


def build_eval_pairs(annotated: list[dict]) -> list[dict]:
    """Build a flat evaluation pair record from each annotated draft."""
    pairs = []
    for item in annotated:
        ann = item["human_annotation"]
        model_scores = ann.get("model_scores", {})
        human_scores = ann.get("human_scores", {})

        pair = {
            "eval_pair_id": ann["annotation_id"],
            "rfp_id": ann["rfp_id"],
            "program_area": ann["program_area"],
            "annotator_id": ann["annotator_id"],
            "annotated_at": ann["annotated_at"],
            "human_gate": ann["human_gate_decision"],
            "model_gate": ann["model_gate_decision"],
            "gate_agreement": ann["human_gate_decision"] == ann["model_gate_decision"],
            "human_failure_reasons": ann["human_failure_reasons"],
            "notes": ann.get("notes", ""),
        }

        for dim in DIMENSIONS:
            pair[f"human_{dim}"] = human_scores.get(dim, None)
            pair[f"model_{dim}"] = model_scores.get(dim, None)
            h = human_scores.get(dim)
            m = model_scores.get(dim)
            if h is not None and m is not None:
                pair[f"delta_{dim}"] = round(h - m, 3)

        # Confusion matrix cell
        h_pass = pair["human_gate"] == "PASS"
        m_pass = pair["model_gate"] == "PASS"
        if h_pass and m_pass:
            pair["confusion_cell"] = "TP"
        elif not h_pass and not m_pass:
            pair["confusion_cell"] = "TN"
        elif h_pass and not m_pass:
            pair["confusion_cell"] = "FN"  # model rejected but human approved
        else:
            pair["confusion_cell"] = "FP"  # model approved but human rejected

        pairs.append(pair)
    return pairs


def build_calibration_report(pairs: list[dict]) -> dict:
    """Calculate per-dimension evaluator bias and mean absolute error."""
    if not pairs:
        return {"error": "no pairs"}

    dim_stats: dict[str, dict[str, Any]] = {}
    for dim in DIMENSIONS:
        deltas = [p[f"delta_{dim}"] for p in pairs if f"delta_{dim}" in p]
        human_scores = [p[f"human_{dim}"] for p in pairs if p.get(f"human_{dim}") is not None]
        model_scores = [p[f"model_{dim}"] for p in pairs if p.get(f"model_{dim}") is not None]

        if not deltas:
            continue

        dim_stats[dim] = {
            "n": len(deltas),
            "mean_delta": round(statistics.mean(deltas), 4),
            "mae": round(statistics.mean(abs(d) for d in deltas), 4),
            "std_delta": round(statistics.stdev(deltas), 4) if len(deltas) > 1 else 0.0,
            "bias_direction": "model_overestimates" if statistics.mean(deltas) < 0 else "model_underestimates",
            "human_mean": round(statistics.mean(human_scores), 4) if human_scores else None,
            "model_mean": round(statistics.mean(model_scores), 4) if model_scores else None,
        }

    # Gate decision analysis
    confusion: dict[str, int] = defaultdict(int)
    for p in pairs:
        confusion[p["confusion_cell"]] += 1

    tp = confusion.get("TP", 0)
    tn = confusion.get("TN", 0)
    fp = confusion.get("FP", 0)
    fn = confusion.get("FN", 0)
    total = tp + tn + fp + fn

    gate_metrics = {
        "total": total,
        "TP": tp, "TN": tn, "FP": fp, "FN": fn,
        "accuracy": round((tp + tn) / total, 4) if total > 0 else None,
        "precision": round(tp / (tp + fp), 4) if (tp + fp) > 0 else None,
        "recall": round(tp / (tp + fn), 4) if (tp + fn) > 0 else None,
        "false_positive_rate": round(fp / (fp + tn), 4) if (fp + tn) > 0 else None,
        "gate_agreement_rate": round(sum(1 for p in pairs if p["gate_agreement"]) / len(pairs), 4),
        "note": "FP = model PASS but human FAIL (most dangerous). FN = model FAIL but human PASS (overly conservative).",
    }

    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "sample_size": len(pairs),
        "dimension_calibration": dim_stats,
        "gate_metrics": gate_metrics,
        "recommendations": _generate_recommendations(dim_stats, gate_metrics),
    }


def _generate_recommendations(dim_stats: dict, gate_metrics: dict) -> list[str]:
    recs = []
    for dim, stats in dim_stats.items():
        if stats["mae"] > 0.15:
            recs.append(
                f"{dim}: MAE={stats['mae']:.3f} — high calibration error; "
                f"model {stats['bias_direction']}. Consider retuning threshold or prompt."
            )
        if abs(stats["mean_delta"]) > 0.10:
            recs.append(
                f"{dim}: systematic bias {stats['mean_delta']:+.3f} — "
                f"{'lower' if stats['mean_delta'] < 0 else 'raise'} model score threshold."
            )
    fpr = gate_metrics.get("false_positive_rate")
    if fpr is not None and fpr > 0.10:
        recs.append(
            f"Gate FPR={fpr:.3f} — model is approving drafts humans would reject. "
            "Raise gate thresholds or add evaluator coverage."
        )
    if not recs:
        recs.append("Calibration looks good — no systematic issues detected.")
    return recs


def main() -> None:
    parser = argparse.ArgumentParser(description="Build evaluation pairs from annotated RFP drafts")
    parser.add_argument("--annotated-dir", type=Path, default=Path("data/golden-annotated"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/golden-dataset"))
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Loading annotated drafts from: {args.annotated_dir}")
    annotated = _load_annotated(args.annotated_dir)
    print(f"Found {len(annotated)} annotated draft(s)")

    if not annotated:
        print("No annotated files found. Run annotator_cli.py first.")
        return

    pairs = build_eval_pairs(annotated)

    # Write JSONL
    pairs_path = args.output_dir / "eval_pairs.jsonl"
    with pairs_path.open("w") as f:
        for pair in pairs:
            f.write(json.dumps(pair) + "\n")
    print(f"Wrote {len(pairs)} eval pairs → {pairs_path}")

    # Write calibration report
    report = build_calibration_report(pairs)
    report_path = args.output_dir / "calibration_report.json"
    report_path.write_text(json.dumps(report, indent=2))
    print(f"Calibration report → {report_path}")

    # Print summary
    print(f"\n{'─' * 50}")
    print("CALIBRATION SUMMARY")
    print(f"{'─' * 50}")
    for dim, stats in report.get("dimension_calibration", {}).items():
        print(f"  {dim:22s}: MAE={stats['mae']:.3f}  bias={stats['mean_delta']:+.3f}  ({stats['bias_direction']})")
    gm = report["gate_metrics"]
    print(f"\n  Gate accuracy: {gm.get('accuracy', 'N/A')}  FPR: {gm.get('false_positive_rate', 'N/A')}")
    print(f"\nRecommendations:")
    for rec in report.get("recommendations", []):
        print(f"  • {rec}")
    print()


if __name__ == "__main__":
    main()
