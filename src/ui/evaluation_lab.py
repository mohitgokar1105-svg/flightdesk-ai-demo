"""AI Evaluation Lab: run approaches on a versioned dataset, compare runs, inspect failures, export results."""

from __future__ import annotations

import json

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import select

from src.ai.evaluation import (
    APPROACHES,
    METRIC_DEFINITIONS,
    METRIC_LIMITATIONS,
    SCORED_FIELDS,
    DatasetError,
    compare_runs_frame,
    estimate_live_cost,
    export_run,
    list_datasets,
    list_runs,
    load_dataset,
    per_field_frame,
    run_cases_frame,
    run_evaluation,
    select_cases,
)
from src.ai.extraction import METHOD_OPENAI
from src.auth import Permission
from src.models import EvaluationCase, EvaluationRun
from src.ui.common import (
    SERIES,
    SINGLE,
    app_settings,
    badge,
    db,
    empty_state,
    fmt_dt,
    fmt_pct,
    live_client,
    metric_row,
    page_header,
    show_exception,
    style_figure,
)

CORRECTIVE_HINTS = {
    "field:passenger_count": "Passenger nouns/compositions (e.g. 'skiers', 'me n 2 friends') — extend lexicon or rely on the model; add cases.",
    "field:departure": "Date phrasing (typos, non-English, 'either X or Y') — clarify window rules in the prompt; add parser patterns.",
    "field:return_date": "Return-date cues — check relative returns ('a week later') stay null rather than guessed.",
    "field:origin": "Unknown/lowercase/misspelled places — gazetteer limits the baseline; the model should keep names as written.",
    "field:destination": "Same as origin; also multi-leg and vague destinations ('the island') must stay null or first stop.",
    "field:trip_type": "Trip-type cues ('1 way', 'ida y vuelta') — prompt states round_trip only when a return is mentioned.",
    "field:date_flexibility": "Flexibility statements that refer to budgets, not dates, or implicit phrasings ('any day in late November').",
    "field:requested_services": "Negation and context ('transfer to the hotel' is not a hotel request) — tighten service rules.",
    "field:trip_purpose": "Only explicit purposes count; implicit ones ('gettin married') need language understanding.",
    "field:budget_amount": "Multiple amounts or typos ('budgt') — deterministic conflict rule may null a correct budget.",
    "field:currency": "No symbol/code means currency must stay null (never assume USD).",
    "unsupported_claim": "A value could not be matched to the text — inspect for assumptions; strengthen 'never infer' instructions.",
    "validation_missed": "A required flag was not raised — extend deterministic checks (e.g. role-play injection) or use the model flag.",
    "validation_extra": "A flag fired that a human would not raise — e.g. '3 of us + 2 colleagues = 5' conflict misfire.",
    "missing_info_false_positive": "Usually a downstream effect of a missed field — fix the field first.",
    "missing_info_false_negative": "A field was filled that should be missing — check for unsupported assumptions.",
    "recommendation_mismatch": "Recommendations follow extracted fields — fix the underlying field error.",
}


def _dataset_picker():
    paths = list_datasets()
    if not paths:
        empty_state("No datasets", "Run `python scripts/build_eval_dataset.py` to create evals/datasets/inquiry_extraction_v1.jsonl.")
        return None
    path = st.selectbox("Dataset", paths, format_func=lambda p: p.name)
    try:
        return load_dataset(path)
    except DatasetError as exc:
        st.error(f"Dataset invalid: {exc}")
        return None


def _run_panel(ds) -> None:
    settings = app_settings()
    with st.container(border=True):
        a, b, c = st.columns([1.3, 1.4, 1])
        approach = a.selectbox("Approach", list(APPROACHES), format_func=lambda k: APPROACHES[k])
        tags = b.multiselect("Only cases with tags", ds.tags)
        limit = c.number_input("Max cases (0 = all)", min_value=0, max_value=len(ds.cases), value=0, step=5)
        cases = select_cases(ds, tags or None, int(limit) or None)
        model_override = None
        client = None
        confirm = True
        if approach == METHOD_OPENAI:
            model_override = st.text_input("Model for this run", value=settings.openai_model,
                                           help="Compare configurations by running the same dataset with different models.")
            client, reason = live_client(model_override.strip() or None)
            if client is None:
                st.warning(f"Live evaluation unavailable: {reason} Normal automated tests never call the API.")
            else:
                est = estimate_live_cost(cases, settings)
                st.warning(f"This will make **{est['cases']} live API calls** to `{client.model}`. Estimated ≈{est['input_tokens']:,} "
                           f"input + {est['output_tokens']:,} output tokens ≈ **${est['estimated_cost_usd']:.2f}** at the configured "
                           "(assumed) prices. Actual usage is recorded per case.")
                confirm = st.checkbox("I understand this run incurs API costs", value=False)
        notes = st.text_input("Run notes (optional)")
        disabled = not cases or (approach == METHOD_OPENAI and (client is None or not confirm))
        if st.button(f"Run evaluation on {len(cases)} case(s)", type="primary", disabled=disabled):
            bar = st.progress(0.0, text="Starting…")

            def progress(i: int, n: int, cid: str) -> None:
                bar.progress(i / n, text=f"{i}/{n} · {cid}")

            try:
                with db() as s:
                    run = run_evaluation(s, settings, ds, approach, client=client, cases=cases, notes=notes or None, progress=progress)
                    st.session_state["eval_run"] = run.id
                    status = run.status
                bar.empty()
                (st.success if status == "completed" else st.error)(f"Run {st.session_state['eval_run']} {status}.")
            except Exception as exc:  # noqa: BLE001
                show_exception(exc)


def _results(run: EvaluationRun) -> None:
    m = run.metrics or {}
    st.markdown(" ".join([badge(run.status, "completed" if run.status == "completed" else "failed"), badge(APPROACHES.get(run.approach, run.approach)),
                          badge(f"model {run.model}" if run.model else "no model"), badge(f"{run.dataset_name} v{run.dataset_version}"),
                          badge(f"{run.n_cases} cases")]), unsafe_allow_html=True)
    if run.error:
        st.error(f"Run stopped early: {run.error}")
    metric_row([
        ("Field accuracy", fmt_pct(m.get("field_accuracy")), f"end-to-end {fmt_pct(m.get('field_accuracy_end_to_end'))}", METRIC_DEFINITIONS["field_accuracy"]),
        ("Exact match", fmt_pct(m.get("exact_match_rate")), "all 11 fields correct", METRIC_DEFINITIONS["exact_match_rate"]),
        ("Missing-info P / R", f"{fmt_pct(m.get('missing_info_precision'))} / {fmt_pct(m.get('missing_info_recall'))}", "micro-averaged",
         METRIC_DEFINITIONS["missing_info_precision"]),
        ("Validation P / R", f"{fmt_pct(m.get('validation_precision'))} / {fmt_pct(m.get('validation_recall'))}", "evaluated codes",
         METRIC_DEFINITIONS["validation_recall"]),
    ])
    st.markdown(" ")
    metric_row([
        ("Schema valid", fmt_pct(m.get("schema_valid_rate")), "raw output", METRIC_DEFINITIONS["schema_valid_rate"]),
        ("Unsupported claims", fmt_pct(m.get("unsupported_claim_rate")), f"{m.get('unsupported_claims', 0)} values", METRIC_DEFINITIONS["unsupported_claim_rate"]),
        ("Recommendation P / R", f"{fmt_pct(m.get('recommendation_precision'))} / {fmt_pct(m.get('recommendation_recall'))}",
         f"{m.get('recommendation_cases', 0)} labelled cases", METRIC_DEFINITIONS["recommendation_precision"]),
        ("Failure rate", fmt_pct(m.get("failure_rate")), f"{m.get('n_succeeded', 0)} succeeded", METRIC_DEFINITIONS["failure_rate"]),
        ("Latency p50 / p95", f"{m.get('latency_ms_p50')} / {m.get('latency_ms_p95')} ms", f"tokens {m.get('input_tokens', 0)}+{m.get('output_tokens', 0)} · ${m.get('estimated_cost_usd', 0):.4f}",
         METRIC_DEFINITIONS["latency_ms_mean/p50/p95"]),
    ])
    left, right = st.columns(2)
    pf = m.get("per_field_accuracy") or {}
    if pf:
        fig = go.Figure(go.Bar(y=list(pf), x=[(v or 0) * 100 for v in pf.values()], orientation="h", marker_color=SINGLE,
                               marker_cornerradius=4, hovertemplate="%{y}: %{x:.1f}%<extra></extra>"))
        fig.update_xaxes(range=[0, 100], ticksuffix="%")
        left.plotly_chart(style_figure(fig, height=340, title="Per-field accuracy"), width="stretch")
    fc = m.get("failure_category_counts") or {}
    if fc:
        items = sorted(fc.items(), key=lambda kv: kv[1])[-14:]
        fig = go.Figure(go.Bar(y=[k for k, _ in items], x=[v for _, v in items], orientation="h", marker_color=SINGLE,
                               marker_cornerradius=4, hovertemplate="%{y}: %{x} cases<extra></extra>"))
        right.plotly_chart(style_figure(fig, height=340, title="Failure categories (cases)"), width="stretch")


def _failures(run: EvaluationRun) -> None:
    with db() as s:
        df = run_cases_frame(s, run.id)
    if df.empty:
        return
    cats = sorted({c.strip() for v in df["failure_categories"] for c in str(v).split(",") if c.strip()})
    a, b = st.columns([2, 1])
    chosen = a.multiselect("Filter by failure category", cats)
    only_fail = b.toggle("Only non-exact cases", value=True)
    view = df
    if only_fail:
        view = view[~view["exact_match"]]
    if chosen:
        view = view[view["failure_categories"].apply(lambda v: any(c in str(v) for c in chosen))]
    st.dataframe(view.drop(columns=["input"]), hide_index=True, width="stretch", height=260)
    if view.empty:
        return
    cid = st.selectbox("Inspect case", view["case_id"].tolist())
    with db() as s:
        c = s.scalar(select(EvaluationCase).where(EvaluationCase.run_id == run.id, EvaluationCase.case_id == cid))
        assert c is not None
        expected, actual, field_results = dict(c.expected), dict(c.actual), dict(c.field_results)
        failures, error, text, tags = list(c.failure_categories), c.error, c.input_text, list(c.tags)
    st.markdown(f"**Input** ({', '.join(tags)})")
    st.code(text, language=None)
    if error:
        st.error(f"Extraction failed: {error}")
    rows = []
    for f in SCORED_FIELDS:
        exp = expected["fields"].get(f)
        act = (actual.get("fields") or {}).get(f)
        if isinstance(act, dict):
            act = {k: v for k, v in act.items() if k != "source_text"}
        rows.append({"field": f, "correct": "✓" if field_results.get(f) else "✗", "expected": json.dumps(exp, default=str),
                     "actual": json.dumps(act, default=str)})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    a, b, c2 = st.columns(3)
    a.markdown("**Missing info**  \n" + f"expected: {expected.get('missing')}  \nactual: {actual.get('missing')}")
    b.markdown("**Validation codes**  \n" + f"expected: {expected.get('validation')}  \nactual: {actual.get('validation')}")
    c2.markdown("**Unsupported values**  \n" + f"{actual.get('unsupported_fields') or 'none'}")
    if expected.get("recommendations") is not None:
        st.markdown(f"**Recommendations** expected {expected['recommendations']} · actual {actual.get('recommendations')}")
    hints = [CORRECTIVE_HINTS[f] for f in failures if f in CORRECTIVE_HINTS]
    if hints:
        st.markdown("**Suggested corrective strategy**")
        for h in dict.fromkeys(hints):
            st.markdown(f"- {h}")
    if actual.get("raw_model_output"):
        with st.expander("Raw model output (validated JSON)"):
            st.json(actual["raw_model_output"], expanded=False)


def _compare() -> None:
    with db() as s:
        runs = [r for r in list_runs(s) if r.approach != "assistant_chat"]
    if len(runs) < 1:
        empty_state("No runs yet", "Run an evaluation above.")
        return
    labels = {f"{r.id} · {r.approach} · {r.model or 'n/a'} · {r.n_cases} cases · {fmt_dt(r.created_at)}": r.id for r in runs}
    default = list(labels)[:2]
    chosen = st.multiselect("Runs to compare (max 3)", list(labels), default=default, max_selections=3)
    sel = [r for r in runs if r.id in {labels[c] for c in chosen}]
    if not sel:
        return
    st.dataframe(compare_runs_frame(sel), hide_index=True, width="stretch")
    if len({r.n_cases for r in sel}) > 1 or len({r.dataset_version for r in sel}) > 1:
        st.warning("These runs used different case sets or dataset versions — differences are not directly comparable.")
    pf = per_field_frame(sel)
    if not pf.empty:
        fig = go.Figure()
        for i, col in enumerate([c for c in pf.columns if c != "field"]):
            fig.add_bar(name=col, x=pf["field"], y=pf[col] * 100, marker_color=SERIES[i], marker_cornerradius=4,
                        hovertemplate="%{x}: %{y:.1f}%<extra>" + col.split(" · ")[0] + "</extra>")
        style_figure(fig, height=340, title="Per-field accuracy by run")
        fig.update_layout(showlegend=True, barmode="group", legend=dict(orientation="h", y=-0.25))
        fig.update_yaxes(range=[0, 100], ticksuffix="%")
        st.plotly_chart(fig, width="stretch")
    st.caption("Only claim an approach is better when the measured difference on the same cases supports it.")


def _extraction_tab() -> None:
    st.caption("Measures how accurately inquiry messages are read into trip details (AI vs the rule-based reader).")
    ds = _dataset_picker()
    if ds is None:
        return
    st.caption(f"**{ds.name}** · version {ds.version} · {len(ds.cases)} cases · {len(ds.tags)} tags · reference date "
               f"{ds.cases[0].reference_date.isoformat()}")
    _run_panel(ds)
    with db() as s:
        runs = [r for r in list_runs(s) if r.approach != "assistant_chat"]
    if not runs:
        empty_state("No evaluation runs yet", "Run the rule-based baseline (free, offline) to see real metrics.")
        return
    t1, t2, t3, t4 = st.tabs(["Results", "Failures", "Compare runs", "Metric definitions"])
    ids = [r.id for r in runs]
    remembered = st.session_state.get("eval_run")
    current: str = remembered if isinstance(remembered, str) and remembered in ids else ids[0]
    with t1:
        rid = st.selectbox("Run", ids, index=ids.index(current), key="res_run",
                           format_func=lambda i: next(f"{r.id} · {r.approach} · {r.model or 'n/a'} · {r.n_cases} cases" for r in runs if r.id == i))
        rid = rid or current
        run = next(r for r in runs if r.id == rid)
        _results(run)
        a, b, _ = st.columns([1, 1, 3])
        for col, fmt in ((a, "csv"), (b, "json")):
            if col.button(f"Export {fmt.upper()}", key=f"exp_{fmt}"):
                with db() as s:
                    path = export_run(s, rid, fmt)
                st.session_state["eval_export"] = (path.name, path.read_bytes(), fmt)
        if st.session_state.get("eval_export"):
            name, data, fmt = st.session_state["eval_export"]
            st.success(f"Written to data/exports/{name}")
            st.download_button(f"Download {name}", data, file_name=name, mime="text/csv" if fmt == "csv" else "application/json")
    with t2:
        run = next(r for r in runs if r.id == st.session_state.get("res_run", current))
        st.caption(f"Run {run.id}")
        _failures(run)
    with t3:
        _compare()
    with t4:
        for k, v in METRIC_DEFINITIONS.items():
            st.markdown(f"**{k}** — {v}")
        st.markdown("**Limitations**")
        for lim in METRIC_LIMITATIONS:
            st.markdown(f"- {lim}")


def render() -> None:
    page_header("AI Evaluation Lab", "Measured behaviour of the AI on versioned, fictional test cases. Every number comes from an "
                "executed test run.", show_demo_note=False, permission=Permission.RUN_EVALUATIONS)
    from src.ui.assistant_eval_ui import render_assistant_tab

    t1, t2 = st.tabs(["🤖 AI Assistant", "📨 Message reading"])
    with t1:
        render_assistant_tab()
    with t2:
        _extraction_tab()
