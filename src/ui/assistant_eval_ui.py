"""AI Evaluation Lab — assistant evaluation tab (runs, results, failures, comparisons, exports)."""

from __future__ import annotations

import json
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import select

from src.ai.assistant_eval import (
    SECURITY_CATEGORIES,
    assistant_runs,
    compare_runs,
    estimate_cost,
    export_assistant_run,
    load_assistant_dataset,
    regression_ids,
    run_assistant_evaluation,
    select_suite,
)
from src.models import EvaluationCase, EvaluationRun
from src.ui.common import (
    SERIES,
    SINGLE,
    app_settings,
    badge,
    db,
    empty_state,
    fmt_dt,
    live_client,
    metric_row,
    show_exception,
    style_figure,
)

SUITES = {"smoke": "Smoke (quick)", "security": "Security & access", "full": "Full suite", "regression": "Regression cases"}


def _pct(rate: dict[str, Any] | None) -> tuple[str, str]:
    if not rate or rate.get("value") is None:
        return "n/a", "no applicable tests"
    return f"{rate['value'] * 100:.1f}%", f"{rate['numerator']} of {rate['denominator']}"


def _tick(bar: Any, i: int, n: int, cid: str) -> None:
    bar.progress(i / n, text=f"{i}/{n} · {cid}")


def _run_panel(ds) -> None:
    settings = app_settings()
    with st.container(border=True):
        st.markdown("**Run an evaluation** — executes the real AI Assistant against fixed fictional test data.")
        a, b, c, d = st.columns([1.4, 1.2, 0.8, 0.9])
        options = [k for k in SUITES if k != "regression" or regression_ids()]
        suite = a.selectbox("Suite", options, format_func=lambda k: f"{SUITES[k]} · {len(select_suite(ds, k))} tests")
        cases = select_suite(ds, suite)
        model = b.text_input("Model", value=settings.openai_model, help="Run the same tests on another model to compare.")
        concurrency = c.number_input("Parallel", min_value=1, max_value=4, value=2, help="Bounded concurrency (max 4).")
        cap = d.number_input("Cost cap (USD)", min_value=0.1, max_value=50.0, value=5.0, step=0.5,
                             help="Stops scheduling new tests once measured spend reaches this estimate.")
        client, reason = live_client(model.strip() or None)
        if client is None:
            st.info(f"Live evaluation needs the AI service: {reason} Automated tests run separately with mocked responses "
                    "(python -m pytest) and never call the API.")
            return
        est = estimate_cost(cases, settings)
        st.warning(f"This runs **{len(cases)} live tests** against `{client.model}` (about 2 API calls each). Estimated "
                   f"**~${est['estimated_cost_usd']:.2f}** at the configured prices (an estimate — actual tokens are measured "
                   "and recorded). API usage is billed to your OpenAI account.")
        ok = st.checkbox("I understand this uses the API and may incur charges", key="eval_ok")
        notes = st.text_input("Notes (optional)", placeholder="e.g. prompt v1 baseline")
        if st.button(f"Run {len(cases)} tests", type="primary", disabled=not ok):
            bar = st.progress(0.0, text="Starting…")
            try:
                with db() as s:
                    run = run_assistant_evaluation(
                        s, settings, cases, client, dataset=ds, suite=suite, concurrency=int(concurrency), max_cost_usd=float(cap),
                        notes=notes or None, progress=lambda i, n, cid: _tick(bar, i, n, cid))
                    st.session_state["aeval_run"] = run.id
                    status = run.status
                bar.empty()
                (st.success if status == "completed" else st.warning)(f"Run {st.session_state['aeval_run']} {status}.")
            except Exception as exc:  # noqa: BLE001
                show_exception(exc)


def _summary(run: EvaluationRun) -> None:
    m = run.metrics or {}
    cfg = run.config or {}
    st.markdown(" ".join([badge(run.status, "completed" if run.status == "completed" else "warn"), badge(f"model {run.model}"),
                          badge(f"{run.dataset_name} v{run.dataset_version}"), badge(f"prompt {cfg.get('prompt_version')}"),
                          badge(f"app {cfg.get('app_version')}"), badge(f"suite {cfg.get('suite')}"),
                          badge(fmt_dt(run.created_at))]), unsafe_allow_html=True)
    crit, sec = m.get("critical_failures", []), m.get("security_failures", [])
    if crit:
        st.error(f"**{len(crit)} critical failure(s):** {', '.join(crit)} — inspect these first.")
    if sec:
        st.error(f"**Security / access failures:** {', '.join(sec)}")
    if not crit and not sec and m.get("n_cases"):
        st.success("No critical or security failures in this run.")
    pr = _pct(m.get("pass_rate"))
    metric_row([
        ("Tests executed", f"{m.get('n_cases', 0)}", f"{m.get('passed', 0)} passed · {m.get('failed', 0)} failed", None),
        ("Overall pass rate", pr[0], pr[1], "Tests where every deterministic check passed."),
        ("Unauthorized-action prevention", *_pct(m.get("unauthorized_action_prevention")), "Write/injection tests with no unapproved writes, forbidden tools or claims."),
        ("Data-access prevention", *_pct(m.get("unauthorized_data_access_prevention")), "Privacy tests where restricted values never appeared."),
        ("Unsupported-claim rate", *_pct(m.get("unsupported_claim_rate")), "Share of tests with a forbidden claim in the answer or draft (lower is better)."),
    ])
    st.markdown(" ")
    metric_row([
        ("Tool selection", *_pct(m.get("tool_selection_accuracy")), "Required tool groups called."),
        ("Tool arguments", *_pct(m.get("tool_argument_correctness")), "Required arguments (periods, IDs, dates) correct."),
        ("Factual correctness", *_pct(m.get("factual_correctness")), "Expected facts present in answers."),
        ("Clarify / refuse correctly", *_pct(m.get("refusal_or_clarification_correctness")), None),
        ("Saved after approval", *_pct(m.get("persistence_success")), "Approved proposals verified in the database."),
    ])
    st.markdown(" ")
    lat = (f"{m.get('latency_ms_median')} ms" if m.get("latency_ms_median") is not None else "n/a")
    p95 = (f"p95 {m.get('latency_ms_p95')} ms" if m.get("latency_ms_p95") is not None else "p95 needs ≥20 samples")
    metric_row([
        ("Median latency", lat, f"{p95} · n={m.get('latency_samples', 0)}", None),
        ("API error rate", *_pct(m.get("api_error_rate")), "Unexpected model API errors."),
        ("Tokens (measured)", f"{m.get('input_tokens', 0):,} in", f"{m.get('output_tokens', 0):,} out", m.get("token_note")),
        ("Estimated cost", f"${m.get('estimated_cost_usd', 0):.3f}", "tokens × configured prices (estimate)", None),
        ("Runner errors", f"{m.get('runner_errors', 0)}", "should be 0", None),
    ])
    by_cat = m.get("by_category") or {}
    if by_cat:
        cats = sorted(by_cat, key=lambda k: by_cat[k]["rate"])
        colors = ["#C2410C" if k in SECURITY_CATEGORIES and by_cat[k]["rate"] < 1 else SINGLE for k in cats]
        fig = go.Figure(go.Bar(y=[k.replace("_", " ") for k in cats], x=[by_cat[k]["rate"] * 100 for k in cats], orientation="h",
                               marker_color=colors, marker_cornerradius=4,
                               text=[f"{by_cat[k]['passed']}/{by_cat[k]['n']}" for k in cats], textposition="outside",
                               hovertemplate="%{y}: %{x:.0f}%<extra></extra>"))
        fig.update_xaxes(range=[0, 115], ticksuffix="%")
        st.plotly_chart(style_figure(fig, height=max(320, 22 * len(cats)), title="Pass rate by category (passed / tests)"),
                        width="stretch")


def _latency(run_id: str) -> None:
    with db() as s:
        lat = [c.duration_ms for c in s.scalars(select(EvaluationCase).where(EvaluationCase.run_id == run_id)) if c.duration_ms]
    if len(lat) >= 5:
        fig = go.Figure(go.Histogram(x=[v / 1000 for v in lat], marker_color=SINGLE, nbinsx=20,
                                     hovertemplate="%{x:.1f}s: %{y} tests<extra></extra>"))
        st.plotly_chart(style_figure(fig, height=240, title="Latency distribution (seconds per test)"), width="stretch")


def _inspect(run_id: str) -> None:
    with db() as s:
        rows = list(s.scalars(select(EvaluationCase).where(EvaluationCase.run_id == run_id).order_by(EvaluationCase.case_id)))
        data = [(c.case_id, c.tags, c.input_text, dict(c.expected), dict(c.actual), dict(c.field_results), list(c.failure_categories),
                 c.exact_match, c.error, c.duration_ms) for c in rows]
    if not data:
        return
    a, b = st.columns([1, 2])
    only_fail = a.toggle("Failures only", value=True, key="aeval_fail_only")
    cats = sorted({d[1][0] for d in data})
    chosen = b.multiselect("Category", cats, key="aeval_cats")
    view = [d for d in data if (not only_fail or not d[7]) and (not chosen or d[1][0] in chosen)]
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    view.sort(key=lambda d: (order.get(d[1][1], 9), d[0]))
    table = pd.DataFrame([{"Test": d[0], "Category": d[1][0].replace("_", " "), "Severity": d[1][1], "Role": d[1][2],
                           "Passed": "✓" if d[7] else "✗", "Failed checks": ", ".join(d[6]), "ms": d[9]} for d in view])
    if table.empty:
        empty_state("Nothing to show", "No tests match these filters.")
        return
    st.dataframe(table, hide_index=True, width="stretch", height=260)
    cid = st.selectbox("Inspect test", [d[0] for d in view], key="aeval_case")
    d = next(x for x in view if x[0] == cid)
    _, tags, prompt, exp, act, checks, failures, passed, error, ms = d
    st.markdown(f"**{cid}** · {tags[0].replace('_', ' ')} · {badge(tags[1], 'bad' if tags[1] == 'critical' else 'neutral')} · "
                f"role {tags[2]} · {badge('passed', 'good') if passed else badge('failed', 'bad')}", unsafe_allow_html=True)
    st.markdown(f"> {prompt}")
    left, right = st.columns(2)
    with left:
        st.markdown("**Expected**")
        if exp.get("criteria"):
            st.caption(exp["criteria"])
        st.markdown("Behaviour: " + ", ".join(exp.get("expected_behavior", [])))
        for f in exp.get("expected_facts", []):
            st.markdown(f"- fact: {f['label']}")
        for g in exp.get("required_tools", []):
            st.markdown(f"- tool: {' or '.join(g['any_of'])}" + (f" with {g['args_any']}" if g.get("args_any") else ""))
        if exp.get("forbidden_patterns"):
            st.markdown(f"- {len(exp['forbidden_patterns'])} forbidden claim pattern(s)")
        if exp.get("expected_proposal"):
            st.markdown(f"- proposal: {exp['expected_proposal']}")
        if exp.get("tool_failures") or exp.get("api_failure"):
            st.markdown(f"- injected failure: {exp.get('api_failure') or ', '.join(exp['tool_failures'])}")
    with right:
        st.markdown("**Actual**")
        if error:
            st.error(error)
        st.markdown(act.get("answer") or "_(no answer)_")
        if act.get("draft_text", "").strip():
            with st.expander("Proposed draft"):
                st.text(act["draft_text"])
        if act.get("forbidden_hits"):
            st.warning("Forbidden claim matched: " + "; ".join(act["forbidden_hits"]))
    st.markdown("**Checks**")
    st.markdown(" ".join(badge(("✓ " if v else "✗ ") + k.replace("_", " "), "good" if v else "bad") for k, v in checks.items()),
                unsafe_allow_html=True)
    calls = act.get("tool_calls", [])
    if calls:
        st.markdown("**Tool calls** (executed by the application; rejections are enforced in code)")
        st.dataframe(pd.DataFrame([{"tool": c["tool"], "status": c["status"], "arguments": json.dumps(c.get("args"), default=str),
                                    "reason": c.get("error") or ""} for c in calls]), hide_index=True, width="stretch")
    st.caption(f"Database writes before approval: {'yes' if act.get('business_writes') else 'none'} · "
               f"approval: {act.get('approval') or 'n/a'} · {ms} ms")


def _history(runs: list[EvaluationRun]) -> None:
    done = [r for r in runs if r.metrics and r.metrics.get("pass_rate", {}).get("value") is not None]
    if len(done) < 2:
        st.caption("Run the evaluation again later (e.g. after a prompt or model change) to see trends.")
        return
    done = list(reversed(done))
    fig = go.Figure()
    fig.add_scatter(x=[r.created_at for r in done], y=[r.metrics["pass_rate"]["value"] * 100 for r in done], mode="lines+markers",
                    name="Overall pass rate", line=dict(color=SERIES[0], width=2), marker=dict(size=8),
                    text=[f"{r.id} · {r.model} · {r.config.get('suite')} · n={r.n_cases}" for r in done],
                    hovertemplate="%{text}<br>%{y:.1f}%<extra></extra>")
    fig.add_scatter(x=[r.created_at for r in done],
                    y=[(r.metrics.get("unauthorized_action_prevention", {}).get("value") or 0) * 100 for r in done],
                    mode="lines+markers", name="Unauthorized-action prevention", line=dict(color=SERIES[1], width=2),
                    marker=dict(size=8), hovertemplate="%{y:.1f}%<extra></extra>")
    style_figure(fig, height=280, title="History across runs")
    fig.update_layout(showlegend=True, legend=dict(orientation="h", y=-0.2))
    fig.update_yaxes(range=[0, 105], ticksuffix="%")
    st.plotly_chart(fig, width="stretch")
    st.caption("Runs on different suites or dataset versions aren't directly comparable — check the suite and n.")


def _compare(runs: list[EvaluationRun]) -> None:
    if len(runs) < 2:
        st.caption("Two runs are needed to compare.")
        return
    labels = {f"{r.id} · {r.model} · {r.config.get('suite')} · {fmt_dt(r.created_at)}": r.id for r in runs}
    a, b = st.columns(2)
    ra = labels[a.selectbox("Baseline run", list(labels), index=1, key="cmp_a")]
    rb = labels[b.selectbox("New run", list(labels), index=0, key="cmp_b")]
    run_a = next(r for r in runs if r.id == ra)
    run_b = next(r for r in runs if r.id == rb)
    keys = ["pass_rate", "unauthorized_action_prevention", "unauthorized_data_access_prevention", "factual_correctness",
            "tool_selection_accuracy", "tool_argument_correctness", "unsupported_claim_rate"]
    rows = []
    for k in keys:
        va = (run_a.metrics or {}).get(k, {}).get("value")
        vb = (run_b.metrics or {}).get(k, {}).get("value")
        rows.append({"metric": k.replace("_", " "), "baseline": va, "new": vb,
                     "change (pts)": None if va is None or vb is None else round((vb - va) * 100, 1)})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    with db() as s:
        diff = pd.DataFrame(compare_runs(s, ra, rb))
    reg = diff[diff["change"] == "regression"]
    if not reg.empty:
        st.error(f"{len(reg)} regression(s): {', '.join(reg['case_id'])} — these passed in the baseline and fail now.")
    st.dataframe(diff[diff["change"] != "unchanged"], hide_index=True, width="stretch")
    st.caption("A new version is better only if it improves correctness and safety on the same tests — not because it reads more fluently.")


def render_assistant_tab() -> None:
    try:
        ds = load_assistant_dataset()
    except Exception as exc:  # noqa: BLE001
        st.error(f"Evaluation dataset invalid: {exc}")
        return
    st.caption(f"Dataset **{ds.name}** v{ds.version} · {len(ds.cases)} tests · {len(ds.categories)} categories · fixed fictional "
               "data at Thu 15 Oct 2026 12:00 UTC. Scores are deterministic checks — no AI judges the AI.")
    _run_panel(ds)
    with db() as s:
        runs = assistant_runs(s)
    if not runs:
        empty_state("No evaluation runs yet", "Run the smoke suite to measure the assistant. Until a run exists, no scores are shown.")
        return
    ids = [r.id for r in runs]
    remembered = st.session_state.get("aeval_run")
    cur: str = remembered if isinstance(remembered, str) and remembered in ids else ids[0]
    rid: str = st.selectbox("Run", ids, index=ids.index(cur), key="aeval_pick",
                       format_func=lambda i: next(f"{r.id} · {r.model} · {r.config.get('suite')} · {r.n_cases} tests · {fmt_dt(r.created_at)}"
                                                  for r in runs if r.id == i)) or cur
    run = next(r for r in runs if r.id == rid)
    t1, t2, t3, t4 = st.tabs(["Results", "Inspect tests", "Compare runs", "History"])
    with t1:
        _summary(run)
        _latency(run.id)
        a, b, _ = st.columns([1, 1, 3])
        for col, fmt in ((a, "csv"), (b, "json")):
            if col.button(f"Export {fmt.upper()}", key=f"aexp_{fmt}"):
                with db() as s:
                    path = export_assistant_run(s, rid, fmt)
                st.session_state["aeval_export"] = (path.name, path.read_bytes(), fmt)
        if st.session_state.get("aeval_export"):
            name, data, fmt = st.session_state["aeval_export"]
            st.download_button(f"Download {name}", data, file_name=name, mime="text/csv" if fmt == "csv" else "application/json")
    with t2:
        _inspect(rid)
    with t3:
        _compare(runs)
    with t4:
        _history(runs)
