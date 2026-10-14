"""Evaluation framework: dataset loading, metric maths, failure recording, model comparison, export."""

from __future__ import annotations

import json
from datetime import date

import pytest

from src.ai.evaluation import (
    DatasetError,
    EvalCaseModel,
    aggregate,
    compare_field,
    compare_runs_frame,
    export_run,
    list_datasets,
    load_dataset,
    run_evaluation,
    score_case,
    select_cases,
)
from src.ai.extraction import ExtractionOutcome, run_rule_based
from src.exceptions import AITransientError
from src.models import EvaluationCase

DATASET = list_datasets()[0]


def test_dataset_loads_and_is_well_formed():
    ds = load_dataset(DATASET)
    assert len(ds.cases) >= 100 and ds.version == "1.0.0"
    required_tags = {"complete", "missing_dates", "ambiguous_location", "pax_conflict", "flexible_dates", "missing_budget",
                     "multi_service", "typos", "unsupported_assumption", "prompt_injection", "malformed"}
    assert required_tags <= set(ds.tags)
    assert len({c.case_id for c in ds.cases}) == len(ds.cases)


def test_dataset_validation_errors_name_the_line(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"case_id": "X-1"}\n', encoding="utf-8")
    with pytest.raises(DatasetError, match="line 1"):
        load_dataset(bad)


def test_field_comparison_rules():
    assert compare_field("origin", "Los Angeles", "LAX")
    assert compare_field("budget_amount", 80000, 80000.4)
    assert not compare_field("budget_amount", 80000, None)
    assert compare_field("requested_services", ["a", "b"], ["b", "a"])
    assert compare_field("departure", {"exact": None, "earliest": "2026-12-05", "latest": "2026-12-05"},
                         {"exact": "2026-12-05", "earliest": None, "latest": None})
    assert compare_field("passenger_count", None, None)


def _case(**over) -> EvalCaseModel:
    base = {"case_id": "T-1", "dataset_version": "t", "reference_date": "2026-10-11", "tags": ["t"],
            "input": "One way Miami to Nassau on November 20 for 5 passengers.",
            "expected": {"origin": "Miami", "destination": "Nassau", "trip_type": "one_way",
                         "departure": {"exact": "2026-11-20"}, "return_date": {}, "passenger_count": 5, "trip_purpose": None,
                         "date_flexibility": None, "budget_amount": None, "currency": None, "requested_services": []},
            "expected_missing": [], "expected_validation": [], "expected_recommendations": []}
    base.update(over)
    return EvalCaseModel.model_validate(base)


def test_scoring_perfect_case_and_metric_maths():
    case = _case()
    r = score_case(case, run_rule_based(case.input, date(2026, 10, 11)))
    assert r.exact_match and r.failure_categories == []
    wrong = _case(case_id="T-2", expected={**case.expected.model_dump(), "passenger_count": 6}, expected_missing=["return_date"])
    r2 = score_case(wrong, run_rule_based(wrong.input, date(2026, 10, 11)))
    assert not r2.exact_match and "field:passenger_count" in r2.failure_categories
    assert r2.metrics["missing_fn"] == 1
    failed = score_case(case, ExtractionOutcome(method="openai", extraction=None, schema_valid=False, latency_ms=10,
                                                error="timeout", error_category="ai_transient"))
    m = aggregate([r, r2, failed])
    assert m["n_cases"] == 3 and m["n_succeeded"] == 2
    assert m["field_accuracy"] == pytest.approx(21 / 22, abs=1e-4)
    assert m["field_accuracy_end_to_end"] == pytest.approx(21 / 33, abs=1e-4)
    assert m["exact_match_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert m["failure_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert m["missing_info_recall"] == 0.0 and m["missing_info_precision"] is None
    assert m["failure_category_counts"]["error:ai_transient"] == 1


def test_rule_based_run_is_stored_and_exportable(session, settings, tmp_path):
    ds = load_dataset(DATASET)
    run = run_evaluation(session, settings, ds, "rule_based", cases=select_cases(ds, limit=20))
    assert run.status == "completed" and run.n_cases == 20 and run.metrics["schema_valid_rate"] == 1.0
    assert session.query(EvaluationCase).filter_by(run_id=run.id).count() == 20
    csv_path = export_run(session, run.id, "csv", directory=tmp_path)
    json_path = export_run(session, run.id, "json", directory=tmp_path)
    assert csv_path.read_text(encoding="utf-8").count("\n") == 21
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["run"]["metrics"]["n_cases"] == 20 and "metric_definitions" in payload["run"]


def test_failures_are_recorded_for_model_runs(session, settings, fake_ai):
    ds = load_dataset(DATASET)
    cases = select_cases(ds, limit=3)
    client = fake_ai(parse_responses=[AITransientError("timed out")] * 3)
    run = run_evaluation(session, settings, ds, "openai", client=client, cases=cases)
    assert run.metrics["failure_rate"] == 1.0 and run.metrics["field_accuracy"] is None
    assert all(c.error for c in session.query(EvaluationCase).filter_by(run_id=run.id))


def test_model_comparison_frame(session, settings, fake_ai):
    ds = load_dataset(DATASET)
    cases = select_cases(ds, case_ids=["EX-003"])
    good = {"origin": "London", "destination": "Nice", "trip_type": "one_way",
            "departure": {"exact": "2027-07-14", "earliest": None, "latest": None, "source_text": "14 July 2027"},
            "return_date": {"exact": None, "earliest": None, "latest": None, "source_text": None}, "passenger_count": 3,
            "passenger_counts_mentioned": [3], "trip_purpose": None, "date_flexibility": None, "budget_amount": 25000,
            "currency": "EUR", "requested_services": [], "ambiguities": [], "inconsistencies": [], "evidence": [],
            "embedded_instructions_detected": False}
    r1 = run_evaluation(session, settings, ds, "openai", client=fake_ai(parse_responses=[good], model="model-a"), cases=cases)
    r2 = run_evaluation(session, settings, ds, "rule_based", cases=cases)
    frame = compare_runs_frame([r1, r2])
    assert list(frame["model"]) == ["model-a", "—"] and frame["exact_match_rate"].tolist() == [1.0, 1.0]
    assert r1.metrics["input_tokens"] == 120 and r1.metrics["estimated_cost_usd"] > 0


def test_openai_approach_requires_client(session, settings):
    with pytest.raises(ValueError):
        run_evaluation(session, settings, load_dataset(DATASET), "openai", client=None)
