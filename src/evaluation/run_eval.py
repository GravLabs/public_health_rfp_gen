"""
Runs the full evaluation pipeline against a draft and logs results to App Insights.
CLI: python run_eval.py --draft-id DRAFT-001 --draft-file output/draft.json --input-file data/input.json
"""

import argparse
import json
import os
from datetime import datetime, timezone
from azure.monitor.opentelemetry import configure_azure_monitor
from opentelemetry import trace

from gate import evaluate_draft, GateResult


APPINSIGHTS_CONN = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--draft-id", required=True)
    parser.add_argument("--draft-file", required=True)
    parser.add_argument("--input-file", required=True)
    args = parser.parse_args()

    configure_azure_monitor(connection_string=APPINSIGHTS_CONN)
    tracer = trace.get_tracer(__name__)

    with open(args.draft_file) as f:
        draft = json.load(f)
    with open(args.input_file) as f:
        input_spec = json.load(f)

    with tracer.start_as_current_span("rfp_evaluation") as span:
        span.set_attribute("draft_id", args.draft_id)
        decision = evaluate_draft(args.draft_id, draft, input_spec)

        # Log all scores as span attributes for App Insights
        for score in decision.scores:
            span.set_attribute(f"eval.{score.metric}", score.score)
            span.set_attribute(f"eval.{score.metric}.passed", score.passed)
        span.set_attribute("eval.gate_result", decision.result.value)

    print(f"\n{'='*60}")
    print(f"DRAFT: {args.draft_id}")
    print(f"GATE RESULT: {decision.result.value}")
    print(f"{'='*60}")
    for score in decision.scores:
        status = "✓" if score.passed else "✗"
        print(f"  {status} {score.metric}: {score.score:.3f} (threshold: {score.threshold})")
        if score.detail:
            print(f"      {score.detail}")

    if decision.blocking_failures:
        print(f"\nBLOCKING FAILURES:")
        for f_ in decision.blocking_failures:
            print(f"  - {f_}")

    exit(0 if decision.result == GateResult.PASS else 1)


if __name__ == "__main__":
    main()
