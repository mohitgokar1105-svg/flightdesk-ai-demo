"""Evaluation framework for inquiry extraction.

Approaches are run over the same versioned JSONL dataset; every metric is computed from the stored
per-case results (nothing is hardcoded). See :data:`METRIC_DEFINITIONS` for exact definitions.
"""

from __future__ import annotations

import csv
import json
import math
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.ai.client import AIClient, TokenUsage, estimate_cost_usd
from src.ai.extraction import METHOD_OPENAI, METHOD_RULE_BASED, ExtractionOutcome, run_openai, run_rule_based
from src.ai.locations import locations_match
from src.ai.prompts import EXTRACTION_INSTRUCTIONS, EXTRACTION_PROMPT_VERSION
from src.ai.validation import EVALUATED_ISSUE_CODES
from src.auth import Permission, require
from src.config import EVAL_DATASET_DIR, EXPORT_DIR, Settings
from src.models import EvaluationCase, EvaluationRun, Inquiry, utcnow
from src.services.recommendation_service import inquiry_rule_candidates

SCORED_FIELDS = ["origin", "destination", "trip_type", "departure", "return_date", "passenger_count", "trip_purpose",
                 "date_flexibility", "budget_amount", "currency", "requested_services"]
APPROACHES = {METHOD_RULE_BASED: "Rule-based baseline (no AI)", METHOD_OPENAI: "OpenAI structured extraction"}

METRIC_DEFINITIONS: dict[str, str] = {
    "field_accuracy": "Correct field comparisons ÷ all field comparisons, over cases that produced an extraction. "
                      "A field is correct when it equals the expected value after normalisation (null = null counts as correct).",
    "field_accuracy_end_to_end": "As field_accuracy, but cases that failed (no extraction) count every field as incorrect.",
    "exact_match_rate": "Cases where all 11 scored fields are correct ÷ all cases (failed cases count as not matching).",
    "missing_info_precision": "Micro-averaged over (case, missing field) pairs: correctly flagged ÷ all flagged.",
    "missing_info_recall": "Micro-averaged: correctly flagged ÷ all expected missing fields.",
    "missing_info_f1 / validation_f1": "2·TP ÷ (2·TP + FP + FN) — the harmonic mean of precision and recall.",
    "validation_precision": "Over evaluated validation codes only: expected codes raised ÷ all evaluated codes raised.",
    "validation_recall": "Expected validation codes raised ÷ all expected validation codes.",
    "schema_valid_rate": "Cases whose raw output passed schema validation ÷ cases attempted. Always 1.0 for the "
                         "rule-based baseline by construction (it emits Python objects).",
    "unsupported_claim_rate": "Extracted non-null values that the deterministic grounding check could not match to the "
                              "inquiry text ÷ all extracted non-null values (services counted individually).",
    "recommendation_precision": "Over cases labelled with expected recommendations: inquiry-rule recommendations produced "
                                "from the extraction that were expected ÷ all produced.",
    "recommendation_recall": "Expected recommendations produced ÷ all expected (labelled cases only).",
    "failure_rate": "Cases with no extraction (API error, schema failure, invalid input) ÷ all cases.",
    "latency_ms_mean/p50/p95": "Wall-clock time per case in milliseconds, including retries.",
    "tokens": "Input/output tokens reported by the provider (0 for the baseline).",
    "estimated_cost_usd": "Tokens × configured per-million-token prices (assumed list prices; verify against OpenAI pricing).",
}
METRIC_LIMITATIONS = [
    "Unsupported-claim detection is lexical: it can flag correct paraphrases (false positives) and cannot detect wrong "
    "values that happen to appear in the text (false negatives).",
    "Missing-information labels are derived from expected fields with the documented business rule, so they measure how "
    "extraction errors propagate, not an independent judgement.",
    "Validation detection is mostly deterministic and shared by both approaches; differences come from extraction "
    "differences and from the model's own injection flag.",
    "The dataset author also wrote the rule-based baseline, which biases results in the baseline's favour on familiar phrasing.",
    "One reference date (2026-10-11) is used for all cases; relative-date handling is only tested against it.",
]


# ---------------------------------------------------------------------------- dataset
class DateLabel(BaseModel):
    exact: str | None = None
    earliest: str | None = None
    latest: str | None = None


class ExpectedFields(BaseModel):
    origin: str | None
    destination: str | None
    trip_type: str | None
    departure: DateLabel
    return_date: DateLabel
    passenger_count: int | None
    trip_purpose: str | None
    date_flexibility: str | None
    budget_amount: float | None
    currency: str | None
    requested_services: list[str]


class EvalCaseModel(BaseModel):
    case_id: str = Field(min_length=3, max_length=40)
    dataset_version: str
    reference_date: date
    input: str
    tags: list[str]
    expected: ExpectedFields
    expected_missing: list[str]
    expected_validation: list[str]
    expected_recommendations: list[str] | None = None
    notes: str | None = None


@dataclass
class Dataset:
    name: str
    path: Path
    version: str
    cases: list[EvalCaseModel]

    @property
    def tags(self) -> list[str]:
        return sorted({t for c in self.cases for t in c.tags})


class DatasetError(ValueError):
    pass


def list_datasets(directory: Path = EVAL_DATASET_DIR) -> list[Path]:
    return sorted(directory.glob("inquiry_extraction_*.jsonl")) if directory.exists() else []


def load_dataset(path: Path) -> Dataset:
    """Load and validate a JSONL dataset. Raises :class:`DatasetError` naming the offending line."""
    cases: list[EvalCaseModel] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8") as fh:
        for n, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                case = EvalCaseModel.model_validate_json(line)
            except ValidationError as exc:
                raise DatasetError(f"{path.name} line {n}: {exc.errors()[0]['loc']} {exc.errors()[0]['msg']}") from exc
            if case.case_id in seen:
                raise DatasetError(f"{path.name} line {n}: duplicate case_id {case.case_id}")
            unknown = set(case.expected_validation) - EVALUATED_ISSUE_CODES
            if unknown:
                raise DatasetError(f"{path.name} line {n}: unknown validation codes {sorted(unknown)}")
            seen.add(case.case_id)
            cases.append(case)
    if not cases:
        raise DatasetError(f"{path.name} contains no cases")
    versions = {c.dataset_version for c in cases}
    if len(versions) != 1:
        raise DatasetError(f"{path.name} mixes dataset versions {sorted(versions)}")
    return Dataset(name=path.stem, path=path, version=versions.pop(), cases=cases)


# ---------------------------------------------------------------------------- scoring
def _norm_date(d: dict[str, Any] | None) -> tuple[str | None, str | None, str | None]:
    d = d or {}
    exact, lo, hi = d.get("exact"), d.get("earliest"), d.get("latest")
    if exact is None and lo and hi and lo == hi:
        exact, lo, hi = lo, None, None
    if exact is not None and lo in (None, exact) and hi in (None, exact):  # exact date + identical window = exact date
        lo = hi = None
    return exact, lo, hi


def compare_field(name: str, expected: Any, actual: Any) -> bool:
    """Field equality after normalisation."""
    if name in ("origin", "destination"):
        return locations_match(expected, actual)
    if name in ("departure", "return_date"):
        return _norm_date(expected) == _norm_date(actual)
    if name == "budget_amount":
        if expected is None or actual is None:
            return expected is None and actual is None
        return math.isclose(float(expected), float(actual), rel_tol=0.005)
    if name == "requested_services":
        return sorted(set(expected or [])) == sorted(set(actual or []))
    if name == "currency":
        return (expected or None) == ((actual or "").upper() or None)
    return expected == actual


def _actual_fields(outcome: ExtractionOutcome) -> dict[str, Any]:
    ex = outcome.extraction
    assert ex is not None
    dump = ex.model_dump(mode="json")
    return {f: dump[f] for f in SCORED_FIELDS}


def recommendation_keys(fields: dict[str, Any], text: str, missing: list[str]) -> list[str]:
    """Inquiry-only recommendation rule keys for an extraction (transient record; nothing is persisted)."""
    inquiry = Inquiry(id="INQ-EVAL", status="new", original_text=text, requested_services=fields.get("requested_services") or [],
                      date_flexibility=fields.get("date_flexibility"), trip_type=fields.get("trip_type"),
                      passenger_count=fields.get("passenger_count"), missing_information=[{"field": m} for m in missing])
    return sorted(c.rule_key for c in inquiry_rule_candidates(inquiry))


@dataclass
class CaseResult:
    case_id: str
    tags: list[str]
    input_text: str
    expected: dict[str, Any]
    actual: dict[str, Any]
    field_results: dict[str, bool]
    exact_match: bool
    metrics: dict[str, Any]
    failure_categories: list[str]
    error: str | None
    duration_ms: float
    usage: TokenUsage = field(default_factory=TokenUsage)
    schema_valid: bool = True


def score_case(case: EvalCaseModel, outcome: ExtractionOutcome) -> CaseResult:
    expected_fields = case.expected.model_dump()
    expected_blob = {"fields": expected_fields, "missing": case.expected_missing, "validation": case.expected_validation,
                     "recommendations": case.expected_recommendations}
    if not outcome.ok:
        return CaseResult(case.case_id, case.tags, case.input, expected_blob, {}, {f: False for f in SCORED_FIELDS}, False,
                          {"ok": False}, [f"error:{outcome.error_category or 'unknown'}"], outcome.error, outcome.latency_ms,
                          outcome.usage, outcome.schema_valid)
    ex = outcome.extraction
    assert ex is not None
    actual_fields = _actual_fields(outcome)
    field_results = {f: compare_field(f, expected_fields[f], actual_fields[f]) for f in SCORED_FIELDS}
    exact = all(field_results.values())

    actual_missing = sorted({m.field for m in ex.missing_information})
    exp_missing = set(case.expected_missing)
    m_tp, m_fp, m_fn = len(exp_missing & set(actual_missing)), len(set(actual_missing) - exp_missing), len(exp_missing - set(actual_missing))

    raised = sorted(ex.issue_codes() & EVALUATED_ISSUE_CODES)
    exp_val = set(case.expected_validation)
    v_tp, v_fp, v_fn = len(exp_val & set(raised)), len(set(raised) - exp_val), len(exp_val - set(raised))

    grounding = ex.grounding
    unsupported = sorted(k for k, ok in grounding.items() if not ok)

    rec_metrics: dict[str, Any] = {}
    produced: list[str] = []
    if case.expected_recommendations is not None:
        produced = recommendation_keys(actual_fields, case.input, actual_missing)
        exp_r = set(case.expected_recommendations)
        rec_metrics = {"rec_tp": len(exp_r & set(produced)), "rec_fp": len(set(produced) - exp_r), "rec_fn": len(exp_r - set(produced))}

    failures = [f"field:{f}" for f, ok in field_results.items() if not ok]
    if m_fp:
        failures.append("missing_info_false_positive")
    if m_fn:
        failures.append("missing_info_false_negative")
    if v_fn:
        failures.append("validation_missed")
    if v_fp:
        failures.append("validation_extra")
    if unsupported:
        failures.append("unsupported_claim")
    if rec_metrics.get("rec_fp") or rec_metrics.get("rec_fn"):
        failures.append("recommendation_mismatch")

    actual_blob = {"fields": actual_fields, "missing": actual_missing, "validation": raised,
                   "all_issue_codes": sorted(ex.issue_codes()), "unsupported_fields": unsupported,
                   "recommendations": produced if case.expected_recommendations is not None else None,
                   "raw_model_output": outcome.raw_output}
    metrics = {"ok": True, "missing_tp": m_tp, "missing_fp": m_fp, "missing_fn": m_fn, "validation_tp": v_tp,
               "validation_fp": v_fp, "validation_fn": v_fn, "claims_total": len(grounding), "claims_unsupported": len(unsupported),
               **rec_metrics}
    return CaseResult(case.case_id, case.tags, case.input, expected_blob, actual_blob, field_results, exact, metrics,
                      failures, None, outcome.latency_ms, outcome.usage, outcome.schema_valid)


def _ratio(num: float, den: float) -> float | None:
    return None if den == 0 else round(num / den, 4)


def _f1(tp: int, fp: int, fn: int) -> float | None:
    """Harmonic mean of precision and recall (None when undefined)."""
    denom = 2 * tp + fp + fn
    return None if denom == 0 else round(2 * tp / denom, 4)


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, max(0, math.ceil(q * len(s)) - 1))
    return round(s[idx], 2)


def aggregate(results: list[CaseResult], settings: Settings | None = None) -> dict[str, Any]:
    """Compute run-level metrics from case results."""
    n = len(results)
    ok = [r for r in results if r.metrics.get("ok")]
    field_total = len(ok) * len(SCORED_FIELDS)
    field_correct = sum(sum(r.field_results.values()) for r in ok)
    per_field = {f: _ratio(sum(r.field_results[f] for r in ok), len(ok)) for f in SCORED_FIELDS}
    s: Counter[str] = Counter()
    for r in ok:
        for k in ("missing_tp", "missing_fp", "missing_fn", "validation_tp", "validation_fp", "validation_fn",
                  "claims_total", "claims_unsupported", "rec_tp", "rec_fp", "rec_fn"):
            s[k] += r.metrics.get(k, 0)
    rec_cases = sum(1 for r in ok if "rec_tp" in r.metrics)
    latencies = [r.duration_ms for r in results]
    usage = sum((r.usage for r in results), TokenUsage())
    fail_counts = Counter(c for r in results for c in r.failure_categories)
    return {
        "n_cases": n,
        "n_succeeded": len(ok),
        "field_accuracy": _ratio(field_correct, field_total),
        "field_accuracy_end_to_end": _ratio(field_correct, n * len(SCORED_FIELDS)),
        "per_field_accuracy": per_field,
        "exact_match_rate": _ratio(sum(r.exact_match for r in results), n),
        "missing_info_precision": _ratio(s["missing_tp"], s["missing_tp"] + s["missing_fp"]),
        "missing_info_recall": _ratio(s["missing_tp"], s["missing_tp"] + s["missing_fn"]),
        "missing_info_f1": _f1(s["missing_tp"], s["missing_fp"], s["missing_fn"]),
        "validation_precision": _ratio(s["validation_tp"], s["validation_tp"] + s["validation_fp"]),
        "validation_recall": _ratio(s["validation_tp"], s["validation_tp"] + s["validation_fn"]),
        "validation_f1": _f1(s["validation_tp"], s["validation_fp"], s["validation_fn"]),
        "schema_valid_rate": _ratio(sum(r.schema_valid for r in results), n),
        "unsupported_claim_rate": _ratio(s["claims_unsupported"], s["claims_total"]),
        "unsupported_claims": int(s["claims_unsupported"]),
        "recommendation_cases": rec_cases,
        "recommendation_precision": _ratio(s["rec_tp"], s["rec_tp"] + s["rec_fp"]),
        "recommendation_recall": _ratio(s["rec_tp"], s["rec_tp"] + s["rec_fn"]),
        "failure_rate": _ratio(n - len(ok), n),
        "latency_ms_mean": round(sum(latencies) / n, 2) if n else None,
        "latency_ms_p50": _pct(latencies, 0.5),
        "latency_ms_p95": _pct(latencies, 0.95),
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "estimated_cost_usd": estimate_cost_usd(usage, settings) if settings and usage.total_tokens else 0.0,
        "failure_category_counts": dict(fail_counts.most_common()),
    }


# ---------------------------------------------------------------------------- running
def select_cases(dataset: Dataset, tags: list[str] | None = None, limit: int | None = None,
                 case_ids: list[str] | None = None) -> list[EvalCaseModel]:
    cases = dataset.cases
    if case_ids:
        wanted = set(case_ids)
        cases = [c for c in cases if c.case_id in wanted]
    if tags:
        cases = [c for c in cases if set(tags) & set(c.tags)]
    if limit:
        cases = cases[: max(1, int(limit))]
    return cases


def estimate_live_cost(cases: list[EvalCaseModel], settings: Settings, output_tokens_per_case: int = 450) -> dict[str, float]:
    """Rough pre-run estimate: ~4 characters per token for instructions + input; fixed output allowance."""
    in_tokens = sum((len(EXTRACTION_INSTRUCTIONS) + len(c.input) + 150) / 4 for c in cases) + 1100 * len(cases)
    usage = TokenUsage(int(in_tokens), output_tokens_per_case * len(cases))
    return {"cases": len(cases), "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
            "estimated_cost_usd": estimate_cost_usd(usage, settings)}


def run_evaluation(session: Session, settings: Settings, dataset: Dataset, approach: str, *,
                   client: AIClient | None = None, cases: list[EvalCaseModel] | None = None, notes: str | None = None,
                   progress: Callable[[int, int, str], None] | None = None) -> EvaluationRun:
    """Run one approach over the dataset (or a subset) and store run + case results."""
    require(Permission.RUN_EVALUATIONS)
    if approach not in APPROACHES:
        raise ValueError(f"Unknown approach {approach}")
    if approach == METHOD_OPENAI and client is None:
        raise ValueError("The OpenAI approach requires a configured live client.")
    cases = cases if cases is not None else dataset.cases
    run = EvaluationRun(dataset_name=dataset.name, dataset_version=dataset.version, approach=approach,
                        model=client.model if (client and approach == METHOD_OPENAI) else None,
                        config={"prompt_version": EXTRACTION_PROMPT_VERSION if approach == METHOD_OPENAI else None,
                                "case_count": len(cases), "subset": len(cases) != len(dataset.cases)},
                        notes=notes, status="running")
    session.add(run)
    session.commit()
    started = time.perf_counter()
    results: list[CaseResult] = []
    try:
        for i, case in enumerate(cases, start=1):
            if progress:
                progress(i, len(cases), case.case_id)
            t0 = time.perf_counter()
            try:
                if approach == METHOD_RULE_BASED:
                    outcome = run_rule_based(case.input, case.reference_date)
                else:
                    assert client is not None
                    outcome = run_openai(case.input, case.reference_date, client)
            except ValueError as exc:  # invalid input (empty/oversized) is a recorded failure, not a crash
                outcome = ExtractionOutcome(method=approach, extraction=None, schema_valid=False,
                                            latency_ms=(time.perf_counter() - t0) * 1000, error=str(exc),
                                            error_category="validation_error")
            result = score_case(case, outcome)
            results.append(result)
            session.add(EvaluationCase(
                run_id=run.id, case_id=case.case_id, tags=case.tags, input_text=case.input, expected=result.expected,
                actual=result.actual, field_results=result.field_results, metrics=result.metrics,
                exact_match=result.exact_match, failure_categories=result.failure_categories, error=result.error,
                duration_ms=round(result.duration_ms, 2), input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens))
            if i % 10 == 0:
                session.commit()
        metrics = aggregate(results, settings)
        run.metrics = metrics
        run.n_cases = len(results)
        run.input_tokens, run.output_tokens = metrics["input_tokens"], metrics["output_tokens"]
        run.estimated_cost_usd = metrics["estimated_cost_usd"]
        run.status = "completed"
    except Exception as exc:  # keep partial results visible; record why the run stopped
        run.status = "failed"
        run.error = str(exc)[:500]
        run.metrics = aggregate(results, settings) if results else {}
        run.n_cases = len(results)
    run.completed_at = utcnow()
    run.duration_ms = int((time.perf_counter() - started) * 1000)
    session.commit()
    return run


# ---------------------------------------------------------------------------- reporting
def list_runs(session: Session) -> list[EvaluationRun]:
    return list(session.scalars(select(EvaluationRun).order_by(EvaluationRun.created_at.desc())))


def run_cases_frame(session: Session, run_id: str) -> pd.DataFrame:
    rows = []
    for c in session.scalars(select(EvaluationCase).where(EvaluationCase.run_id == run_id).order_by(EvaluationCase.case_id)):
        rows.append({"case_id": c.case_id, "exact_match": c.exact_match, "fields_correct": sum(c.field_results.values()),
                     "failure_categories": ", ".join(c.failure_categories), "error": c.error, "tags": ", ".join(c.tags),
                     "duration_ms": c.duration_ms, "input_tokens": c.input_tokens, "output_tokens": c.output_tokens,
                     "input": c.input_text})
    return pd.DataFrame(rows)


def compare_runs_frame(runs: list[EvaluationRun]) -> pd.DataFrame:
    keys = ["field_accuracy", "field_accuracy_end_to_end", "exact_match_rate", "missing_info_precision", "missing_info_recall",
            "validation_precision", "validation_recall", "schema_valid_rate", "unsupported_claim_rate",
            "recommendation_precision", "recommendation_recall", "failure_rate", "latency_ms_mean", "latency_ms_p95",
            "input_tokens", "output_tokens", "estimated_cost_usd"]
    rows = []
    for r in runs:
        row: dict[str, Any] = {"run_id": r.id, "approach": r.approach, "model": r.model or "—",
                               "prompt": (r.config or {}).get("prompt_version") or "—", "dataset": f"{r.dataset_name} v{r.dataset_version}",
                               "cases": r.n_cases, "status": r.status, "created_at": r.created_at}
        row.update({k: (r.metrics or {}).get(k) for k in keys})
        rows.append(row)
    return pd.DataFrame(rows)


def per_field_frame(runs: list[EvaluationRun]) -> pd.DataFrame:
    data: dict[str, dict[str, Any]] = defaultdict(dict)
    for r in runs:
        label = f"{r.approach} · {r.model or 'n/a'} · {r.id}"
        for f, v in ((r.metrics or {}).get("per_field_accuracy") or {}).items():
            data[f][label] = v
    return pd.DataFrame(data).T.reset_index(names="field") if data else pd.DataFrame()


def export_run(session: Session, run_id: str, fmt: str = "csv", directory: Path = EXPORT_DIR) -> Path:
    """Write a run's case results (CSV) or full run + cases (JSON) under data/exports/."""
    require(Permission.RUN_EVALUATIONS)
    run = session.get(EvaluationRun, run_id)
    if run is None:
        raise ValueError(f"Evaluation run {run_id} not found")
    directory.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    cases = list(session.scalars(select(EvaluationCase).where(EvaluationCase.run_id == run_id).order_by(EvaluationCase.case_id)))
    if fmt == "json":
        path = directory / f"evaluation_{run_id}_{stamp}.json"
        payload = {
            "run": {"id": run.id, "dataset": run.dataset_name, "dataset_version": run.dataset_version, "approach": run.approach,
                    "model": run.model, "config": run.config, "status": run.status, "created_at": run.created_at.isoformat(),
                    "metrics": run.metrics, "metric_definitions": METRIC_DEFINITIONS, "limitations": METRIC_LIMITATIONS},
            "cases": [{"case_id": c.case_id, "tags": c.tags, "input": c.input_text, "expected": c.expected, "actual": c.actual,
                       "field_results": c.field_results, "exact_match": c.exact_match, "failure_categories": c.failure_categories,
                       "error": c.error, "duration_ms": c.duration_ms, "input_tokens": c.input_tokens,
                       "output_tokens": c.output_tokens} for c in cases],
        }
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return path
    path = directory / f"evaluation_{run_id}_{stamp}.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["run_id", "approach", "model", "case_id", "tags", "exact_match", *[f"ok_{f}" for f in SCORED_FIELDS],
                         "failure_categories", "error", "duration_ms", "input_tokens", "output_tokens"])
        for c in cases:
            writer.writerow([run.id, run.approach, run.model or "", c.case_id, "|".join(c.tags), c.exact_match,
                             *[c.field_results.get(f) for f in SCORED_FIELDS], "|".join(c.failure_categories), c.error or "",
                             c.duration_ms, c.input_tokens, c.output_tokens])
    return path
