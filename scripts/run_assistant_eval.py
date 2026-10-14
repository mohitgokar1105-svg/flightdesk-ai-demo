"""Run the AI Assistant evaluation (live OpenAI calls; costs money) and store the results.

Usage:
    python scripts/run_assistant_eval.py --suite smoke              # ~23 tests; asks before spending
    python scripts/run_assistant_eval.py --suite full --yes         # all tests, no prompt
    python scripts/run_assistant_eval.py --suite security --model gpt-4.1-mini --export json

Requires DEMO_MODE=false and OPENAI_API_KEY (from .env or the environment).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ai.assistant_eval import (  # noqa: E402
    estimate_cost,
    export_assistant_run,
    load_assistant_dataset,
    run_assistant_evaluation,
    select_suite,
)
from src.ai.client import build_ai_client  # noqa: E402
from src.auth import SYSTEM, as_principal  # noqa: E402
from src.config import get_settings  # noqa: E402
from src.database import init_db, session_scope  # noqa: E402
from src.exceptions import AINotConfigured  # noqa: E402
from src.logging_config import configure_logging  # noqa: E402

KEYS = ["pass_rate", "unauthorized_action_prevention", "unauthorized_data_access_prevention", "unsupported_claim_rate",
        "tool_selection_accuracy", "tool_argument_correctness", "factual_correctness", "refusal_or_clarification_correctness",
        "persistence_success", "api_error_rate"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--suite", choices=["smoke", "security", "full", "regression"], default="smoke")
    parser.add_argument("--cases", nargs="*", help="run specific case IDs")
    parser.add_argument("--model")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--max-cost", type=float, default=5.0)
    parser.add_argument("--notes")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--export", choices=["csv", "json"])
    args = parser.parse_args()
    configure_logging(to_file=False)
    settings = get_settings()
    init_db(settings.database_url)
    try:
        client = build_ai_client(settings)
    except AINotConfigured as exc:
        print(f"Cannot run a live evaluation: {exc}")
        return 2
    if args.model:
        client.model = args.model
    ds = load_assistant_dataset()
    cases = select_suite(ds, args.suite, args.cases)
    est = estimate_cost(cases, settings)
    print(f"{len(cases)} live tests on {client.model}; estimated ~${est['estimated_cost_usd']:.2f} (estimate; cap ${args.max_cost}).")
    if not args.yes and input("This uses the OpenAI API and may incur charges. Proceed? [y/N] ").strip().lower() != "y":
        print("Cancelled.")
        return 1
    with session_scope(settings.database_url) as s, as_principal(SYSTEM):
        run = run_assistant_evaluation(s, settings, cases, client, dataset=ds, suite=args.suite if not args.cases else "custom",
                                       concurrency=args.concurrency, max_cost_usd=args.max_cost, notes=args.notes,
                                       progress=lambda i, n, cid: print(f"\r{i}/{n} {cid}   ", end="", flush=True))
        print()
        m = run.metrics
        print(f"Run {run.id} [{run.status}] {m.get('passed')}/{m.get('n_cases')} passed")
        print(json.dumps({k: m.get(k) for k in KEYS}, indent=2))
        print("Critical failures:", m.get("critical_failures") or "none")
        print("Security failures:", m.get("security_failures") or "none")
        print(f"Tokens: {m.get('input_tokens')} in / {m.get('output_tokens')} out · estimated cost ${m.get('estimated_cost_usd')}")
        if args.export:
            print("Exported:", export_assistant_run(s, run.id, args.export))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
