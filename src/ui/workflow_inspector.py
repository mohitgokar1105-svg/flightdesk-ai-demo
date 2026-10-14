"""AI Workflow Inspector: runs, state transitions, model vs. application vs. tool vs. human events, approvals."""

from __future__ import annotations

import html
import json
from typing import Any

import pandas as pd
import streamlit as st
from openai.lib._pydantic import to_strict_json_schema
from sqlalchemy import select

from src.auth import Permission
from src.models import ActionProposal, WorkflowEvent, WorkflowRun
from src.ui.common import app_settings, badge, db, empty_state, fmt_dt, metric_row, page_header, source_badge
from src.workflows import workflow_engine as wf
from src.workflows.state_machine import TRANSITIONS, WORKFLOW_DESCRIPTIONS, WorkflowState, WorkflowType
from src.workflows.tools import ASSISTANT_ALLOWLIST, REGISTRY, TRIAGE_ALLOWLIST

HIDDEN_CONTEXT_KEYS = {"inquiry_text"}  # never display full customer messages here


def _state_diagram() -> str:
    lines = ["digraph G {", 'rankdir=LR; node [shape=box, style="rounded,filled", fillcolor="#F8FAFC", color="#CBD5E1", '
             'fontname="Helvetica", fontsize=10]; edge [color="#94A3B8", arrowsize=0.6];']
    for s in WorkflowState:
        if s in (WorkflowState.COMPLETED,):
            lines.append(f'{s.value} [fillcolor="#DCFCE7"];')
        elif s in (WorkflowState.FAILED, WorkflowState.REJECTED):
            lines.append(f'{s.value} [fillcolor="#FEE2E2"];')
        elif s == WorkflowState.AWAITING_APPROVAL:
            lines.append(f'{s.value} [fillcolor="#FEF3C7"];')
    for src, targets in TRANSITIONS.items():
        for t in sorted(targets):
            if t in (WorkflowState.FAILED, WorkflowState.CANCELLED) and src not in (WorkflowState.RECEIVED,):
                continue  # keep the picture readable; every non-terminal state may fail/cancel
            lines.append(f"{src.value} -> {t.value};")
    lines.append("}")
    return "\n".join(lines)


def _runs_table() -> str | None:
    a, b, c = st.columns(3)
    types = a.multiselect("Workflow type", [t.value for t in WorkflowType])
    states = b.multiselect("State", [s.value for s in WorkflowState])
    sample = c.selectbox("Records", ["All", "Demo session only", "Seeded samples only"])
    with db() as s:
        stmt = select(WorkflowRun).order_by(WorkflowRun.started_at.desc()).limit(300)
        runs = list(s.scalars(stmt))
        rows = [{"run_id": r.id, "type": r.workflow_type, "state": r.state, "started": fmt_dt(r.started_at),
                 "duration_ms": r.duration_ms, "ai_mode": r.ai_mode, "model": r.model or "—", "tokens": r.total_tokens,
                 "est_cost_usd": r.estimated_cost_usd, "inquiry": r.inquiry_id or "—", "error": r.error_category or "",
                 "sample": r.is_sample}
                for r in runs if (not types or r.workflow_type in types) and (not states or r.state in states)
                and (sample == "All" or r.is_sample == (sample == "Seeded samples only"))]
    if not rows:
        empty_state("No workflow runs", "Run an extraction, draft, triage or booking workflow to see it here.")
        return None
    df = pd.DataFrame(rows)
    st.dataframe(df, hide_index=True, width="stretch", height=260)
    preselect = st.session_state.get("inspect_run")
    ids = df["run_id"].tolist()
    return st.selectbox("Inspect run", ids, index=ids.index(preselect) if preselect in ids else 0)


def _run_detail(run_id: str) -> None:
    with db() as s:
        run = s.get(WorkflowRun, run_id)
        assert run is not None
        events = list(s.scalars(select(WorkflowEvent).where(WorkflowEvent.run_id == run_id).order_by(WorkflowEvent.seq)))
        proposals = list(s.scalars(select(ActionProposal).where(ActionProposal.run_id == run_id).order_by(ActionProposal.created_at)))
        ev = [(e.seq, e.timestamp, e.kind, e.source, e.summary, dict(e.payload or {})) for e in events]
        props = [(p.id, p.tool_name, dict(p.arguments or {}), p.status, p.decided_at, p.decided_by, dict(p.result or {}), p.error)
                 for p in proposals]
        info: dict[str, Any] = {"type": run.workflow_type, "state": run.state, "started": run.started_at, "completed": run.completed_at,
                "duration": run.duration_ms, "model": run.model, "mode": run.ai_mode, "in": run.input_tokens, "out": run.output_tokens,
                "cost": run.estimated_cost_usd, "err_cat": run.error_category, "err": run.error_message, "outcome": run.final_outcome,
                "summary": run.input_summary, "inquiry": run.inquiry_id,
                "context": {k: v for k, v in (run.context or {}).items() if k not in HIDDEN_CONTEXT_KEYS}}
    st.markdown(" ".join([badge(info["state"], info["state"]), badge(info["type"]), badge(f"mode: {info['mode']}")]),
                unsafe_allow_html=True)
    st.caption(WORKFLOW_DESCRIPTIONS.get(WorkflowType(info["type"]), ""))
    metric_row([
        ("Duration", f"{info['duration']} ms" if info["duration"] is not None else "in progress", fmt_dt(info["started"]), None),
        ("Model", info["model"] or "none", "configurable via OPENAI_MODEL", None),
        ("Tokens", f"{info['in']} in / {info['out']} out", "reported by provider", None),
        ("Est. cost", f"${info['cost']:.4f}" if info["cost"] else "—", "assumed list prices", None),
        ("Outcome", info["state"], ((info["outcome"] or "in progress")[:70]), info["outcome"]),
    ])
    st.markdown(f"**Input summary:** {html.escape(info['summary'] or '—')}  ·  **Inquiry:** {info['inquiry'] or '—'}")
    if info["err"]:
        st.error(f"{info['err_cat']}: {info['err']}")

    t1, t2, t3, t4, t5 = st.tabs(["Timeline", "State transitions", "Tool calls", "Approvals", "Results & validation"])
    with t1:
        st.markdown(" ".join(source_badge(x) for x in ("model", "application", "tool", "human")) +
                    "  <span style='color:#64748B;font-size:0.8rem'>— each event is attributed to its source</span>",
                    unsafe_allow_html=True)
        for seq, ts, kind, source, summary, payload in ev:
            st.markdown(f'<div class="fd-event"><div class="meta">#{seq} · {ts.strftime("%H:%M:%S.%f")[:-3]} UTC · {kind} · '
                        f'{source_badge(source)}</div>{html.escape(summary)}</div>', unsafe_allow_html=True)
            if payload:
                with st.expander(f"payload #{seq}", expanded=False):
                    st.json(payload, expanded=False)
    with t2:
        trans = [{"#": seq, "time": fmt_dt(ts), "from": p.get("from", "—"), "to": p.get("to"), "reason": p.get("reason", "")}
                 for seq, ts, kind, _, _, p in ev if kind == "STATE_TRANSITION"]
        st.dataframe(pd.DataFrame(trans), hide_index=True, width="stretch")
    with t3:
        calls = [{"#": seq, "kind": kind, "source": src, "tool": p.get("tool"), "arguments": json.dumps(p.get("arguments"), default=str)[:200]
                  if p.get("arguments") is not None else "", "detail": p.get("reason") or (p.get("result_preview") or "")[:200]}
                 for seq, _, kind, src, _, p in ev if kind.startswith("TOOL_CALL") or kind == "PROPOSAL_CREATED"]
        if calls:
            st.dataframe(pd.DataFrame(calls), hide_index=True, width="stretch")
            n_req = sum(1 for c in calls if c["kind"] == "TOOL_CALL_REQUESTED")
            n_rej = sum(1 for c in calls if c["kind"] == "TOOL_CALL_REJECTED")
            st.caption(f"{n_req} accepted request(s), {n_rej} rejected by Python validation/permissions.")
        else:
            st.caption("This run made no tool calls.")
    with t4:
        if not props:
            st.caption("No proposals in this run.")
        for pid, tool, args, status, decided_at, decided_by, result, error in props:
            with st.container(border=True):
                st.markdown(f"{badge(status, status)} **{tool}** · {pid}", unsafe_allow_html=True)
                st.json({k: v for k, v in args.items() if k != "draft_body"}, expanded=False)
                if decided_at:
                    st.caption(f"Decided by {decided_by} at {fmt_dt(decided_at)}")
                if result:
                    st.json(result, expanded=False)
                if error:
                    st.caption(f"Note: {error}")
                if status == "pending" and info["state"] == "AWAITING_APPROVAL":
                    a, b, _ = st.columns([1, 1, 4])
                    if a.button("Approve", key=f"i_ap_{pid}", type="primary"):
                        with db() as s:
                            wf.decide_proposal(s, app_settings(), pid, approve=True)
                        st.session_state["inspect_run"] = run_id
                        st.rerun()
                    if b.button("Reject", key=f"i_rj_{pid}"):
                        with db() as s:
                            wf.decide_proposal(s, app_settings(), pid, approve=False, note="Rejected in Inspector")
                        st.session_state["inspect_run"] = run_id
                        st.rerun()
    with t5:
        st.caption("Stored run context (model outputs, deterministic validation results, drafts). Full customer messages are "
                   "not displayed here; hidden model reasoning is never stored.")
        st.json(info["context"], expanded=False)


def render() -> None:
    page_header("AI activity log", "For technical review: every AI run, state change, tool call and approval is recorded here.", show_demo_note=False,
                permission=Permission.VIEW_DIAGNOSTICS)
    run_id = _runs_table()
    if run_id:
        _run_detail(run_id)
    st.divider()
    left, right = st.columns([1.1, 1])
    with left:
        st.subheader("State machine")
        st.graphviz_chart(_state_diagram())
        st.caption("Every non-terminal state may also transition to FAILED or CANCELLED. Transitions are validated in Python; "
                   "runs are bounded (max transitions, max agent turns, max tool calls).")
    with right:
        st.subheader("Tool registry")
        rows = [{"tool": t.name, "permission": t.permission.value, "mutates": t.mutates, "exposed to model": t.exposed_to_model,
                 "triage agent": t.name in TRIAGE_ALLOWLIST, "read-only assistant": t.name in ASSISTANT_ALLOWLIST,
                 "description": t.description} for t in REGISTRY.specs()]
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        name = st.selectbox("Input schema", [t.name for t in REGISTRY.specs()])
        spec = REGISTRY.get(name)
        assert spec is not None
        st.json(to_strict_json_schema(spec.input_model), expanded=False)
