"""Run an extraction evaluation from the command line and store it in the database.

Usage:
    python scripts/run_evaluation.py --approach rule_based
    python scripts/run_evaluation.py --approach openai --limit 20 --model gpt-4.1     # live, costs money; asks first
    python scripts/run_evaluation.py --approach openai --tags prompt_injection --yes  # skip the cost prompt

The OpenAI approach requires DEMO_MODE=false and OPENAI_API_KEY to be set.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ai.client import build_ai_client  # noqa: E402
from src.ai.evaluation import (  # noqa: E402
    APPROACHES,
    estimate_live_cost,
    export_run,
    list_datasets,
    load_dataset,
    run_evaluation,
    select_cases,
)
from src.auth import SYSTEM, as_principal  # noqa: E402
from src.config import get_settings  # noqa: E402
from src.database import init_db, session_scope  # noqa: E402
from src.exceptions import AINotConfigured  # noqa: E402
from src.logging_config import configure_logging  # noqa: E402

SUMMARY_KEYS = ["field_accuracy", "field_accuracy_end_to_end", "exact_match_rate", "missing_info_precision",
                "missing_info_recall", "validation_precision", "validation_recall", "schema_valid_rate",
                "unsupported_claim_rate", "recommendation_precision", "recommendation_recall", "failure_rate",
                "latency_ms_p50", "latency_ms_p95", "input_tokens", "output_tokens", "estimated_cost_usd"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--approach", choices=list(APPROACHES), default="rule_based")
    parser.add_argument("--dataset", type=Path, default=None, help="JSONL dataset (default: first in evals/datasets)")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--tags", nargs="*", default=None)
    parser.add_argument("--model", default=None, help="override OPENAI_MODEL for this run")
    parser.add_argument("--yes", action="store_true", help="do not ask for cost confirmation")
    parser.add_argument("--export", choices=["csv", "json"], default=None)
    args = parser.parse_args()
    configure_logging(to_file=False)
    settings = get_settings()
    init_db(settings.database_url)
    dataset = load_dataset(args.dataset or list_datasets()[0])
    cases = select_cases(dataset, args.tags, args.limit)
    client = None
    if args.approach == "openai":
        try:
            client = build_ai_client(settings)
        except AINotConfigured as exc:
            print(f"Cannot run the OpenAI approach: {exc}")
            return 2
        if args.model:
            client.model = args.model
        est = estimate_live_cost(cases, settings)
        print(f"{len(cases)} live calls to {client.model}; estimated ~${est['estimated_cost_usd']:.2f} "
              f"(assumed prices; actual usage is recorded).")
        if not args.yes and input("Proceed? [y/N] ").strip().lower() != "y":
            print("Cancelled.")
            return 1
    with session_scope(settings.database_url) as s, as_principal(SYSTEM):  # trusted command-line run
        run = run_evaluation(s, settings, dataset, args.approach, client=client, cases=cases,
                             progress=lambda i, n, cid: print(f"\r{i}/{n} {cid}   ", end="", flush=True))
        print()
        print(f"Run {run.id} [{run.status}] {dataset.name} v{dataset.version} · {run.n_cases} cases · approach={run.approach} "
              f"model={run.model or '-'}")
        print(json.dumps({k: run.metrics.get(k) for k in SUMMARY_KEYS}, indent=2))
        if args.export:
            print("Exported:", export_run(s, run.id, args.export))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
