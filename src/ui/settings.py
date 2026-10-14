"""Settings: AI assistant setup, your data (samples, reset, import), and technical health details."""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st
from sqlalchemy import select, text

from src.auth import Permission, has_permission
from src.config import APP_NAME, APP_VERSION, ENV_FILE, TEST_STATUS_FILE
from src.database import RESET_CONFIRMATION_TOKEN, get_engine, get_schema_version, record_counts
from src.models import AuditEvent, WorkflowRun
from src.schemas import LLMExplanation
from src.seed import reset_demo_data, sample_data_present, seed_demo_data
from src.services.audit_service import recent_audit_events, record_audit
from src.services.csv_service import (
    IMPORTABLE,
    commit_import,
    preview_frame,
    read_csv_upload,
    validate_import,
)
from src.ui.common import (
    ai_status,
    app_settings,
    badge,
    db,
    ensure_principal,
    fmt_dt,
    kv_panel,
    live_client,
    page_header,
    show_exception,
    sign_out,
)


def _ai_section() -> None:
    st.markdown("### AI assistant")
    settings = app_settings()
    ready, status = ai_status()
    source = ".env file" if ENV_FILE.exists() and "OPENAI_API_KEY" in ENV_FILE.read_text(encoding="utf-8") else "environment"
    kv_panel([("Status", badge("On", "good") if ready else badge("Off", "neutral")), ("Model", settings.openai_model),
              ("API key", f"configured (from {source})" if settings.ai_key_configured else "not configured"),
              ("Demo mode", "on — AI calls disabled" if settings.demo_mode else "off")])
    st.caption(status)
    with db() as s:
        last_ok = s.scalar(select(AuditEvent).where(AuditEvent.event_type == "ai_request", AuditEvent.outcome == "success")
                           .order_by(AuditEvent.timestamp.desc()).limit(1))
        last_fail = s.scalar(select(AuditEvent).where(AuditEvent.event_type == "ai_request", AuditEvent.outcome == "failure")
                             .order_by(AuditEvent.timestamp.desc()).limit(1))
        st.caption(f"Last successful AI request: {fmt_dt(last_ok.timestamp) if last_ok else 'none yet'}"
                   + (f" · last failure: {fmt_dt(last_fail.timestamp)} ({(last_fail.event_metadata or {}).get('error_category')})"
                      if last_fail else ""))
    client, reason = live_client()
    a, _ = st.columns([1, 3])
    if client is not None and a.button("Test the connection"):
        try:
            res = client.parse(instructions="Reply with one short sentence confirming you are reachable.",
                               user_input="<facts>{\"check\": \"connection test\"}</facts>", schema=LLMExplanation,
                               purpose="connection_test")
            with db() as s:
                record_audit(s, "ai_request", actor="system", metadata={"purpose": "connection_test", "model": res.model,
                                                                        "input_tokens": res.usage.input_tokens,
                                                                        "output_tokens": res.usage.output_tokens})
            st.success(f"Connected to {res.model} in {res.latency_ms:.0f} ms.")
        except Exception as exc:  # noqa: BLE001
            show_exception(exc)
    if client is None:
        with st.expander("How to switch it on"):
            st.markdown("1. Create an API key at platform.openai.com.\n"
                        "2. In the project folder, create or open `.env` (see `.env.example`) and set "
                        "`OPENAI_API_KEY=<your key>` and `DEMO_MODE=false`.\n"
                        "3. Restart FlightDesk.\n\nThe key stays on this computer: it is never displayed, logged or saved in "
                        "the database. Reason it's off right now: " + reason)


def _data_section() -> None:
    st.markdown("### Your data")
    settings = app_settings()
    a, b = st.columns(2)
    with a, st.container(border=True):
        st.markdown("**Sample data**")
        st.caption("Adds fictional customers, quotes and bookings so you can explore. Never deletes anything.")
        if st.button("Load sample data"):
            try:
                with db() as s:
                    res = seed_demo_data(s, settings)
                st.success("Sample data is already loaded." if res.get("skipped") else "Sample data added.")
            except Exception as exc:  # noqa: BLE001
                show_exception(exc)
    with b, st.container(border=True):
        st.markdown("**Start over**")
        st.caption("Permanently deletes every record — including ones you created — and reloads the sample data.")
        token = st.text_input(f"Type {RESET_CONFIRMATION_TOKEN} to confirm", key="reset_token")
        sure = st.checkbox("I understand this can't be undone")
        if st.button("Delete everything and start over", disabled=not (sure and token == RESET_CONFIRMATION_TOKEN)):
            try:
                with db() as s:
                    res = reset_demo_data(s, token, settings)
                st.session_state.clear()
                st.success(f"Deleted {sum(res['deleted'].values())} records and reloaded the sample data.")
            except Exception as exc:  # noqa: BLE001
                show_exception(exc)
    with st.expander("Import from a spreadsheet (CSV)"):
        st.caption("Rows are checked and previewed before anything is saved. Examples are in data/samples/.")
        entity = st.selectbox("Import", IMPORTABLE, format_func=str.capitalize)
        upload = st.file_uploader("CSV file", type=["csv"], key=f"upload_{entity}")
        if upload is not None:
            try:
                df = read_csv_upload(upload.getvalue())
                with db() as s:
                    results = validate_import(s, entity, df)
            except Exception as exc:  # noqa: BLE001
                show_exception(exc)
                return
            preview = preview_frame(results)
            counts = preview["status"].value_counts().to_dict()
            st.markdown(" ".join(badge(f"{k}: {v}", {"valid": "good", "invalid": "bad", "duplicate": "neutral",
                                                          "update": "warn"}.get(k, "neutral")) for k, v in counts.items()),
                        unsafe_allow_html=True)
            st.dataframe(preview, hide_index=True, width="stretch")
            overwrite = bool(counts.get("update")) and st.checkbox(f"Overwrite {counts.get('update', 0)} existing record(s)")
            n = counts.get("valid", 0) + (counts.get("update", 0) if overwrite else 0)
            if st.button(f"Import {n} row(s)", type="primary", disabled=n == 0):
                try:
                    with db() as s:
                        summary = commit_import(s, entity, results, overwrite=overwrite)
                    st.success(f"Imported: {summary['inserted']} new, {summary['updated']} updated. "
                               f"Skipped: {summary['skipped_duplicates']} duplicates, {summary['skipped_invalid']} invalid.")
                except Exception as exc:  # noqa: BLE001
                    show_exception(exc)
                    st.caption("Nothing from this file was saved.")


def _health_section() -> None:
    with st.expander("Technical details"):
        settings = app_settings()
        kv_panel([("App", f"{APP_NAME} {APP_VERSION}"), ("Environment", settings.app_env)])
        try:
            engine = get_engine(settings.database_url)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            with db() as s:
                counts = record_counts(s)
                samples = sample_data_present(s)
            kv_panel([("Database", badge("connected", "good")), ("Schema version", get_schema_version(engine)),
                      ("Location", settings.database_url.replace("sqlite:///", "")), ("Sample data", "loaded" if samples else "not loaded")])
            st.dataframe(pd.DataFrame([{"table": k, "records": v} for k, v in counts.items()]), hide_index=True, width="stretch")
        except Exception as exc:  # noqa: BLE001
            st.error(f"Database unreachable ({type(exc).__name__}).")
        if TEST_STATUS_FILE.exists():
            data = json.loads(TEST_STATUS_FILE.read_text(encoding="utf-8"))
            c = data.get("counts", {})
            st.markdown(f"**Automated tests:** last run {data.get('finished_at', '')[:16].replace('T', ' ')} UTC — "
                        f"{c.get('passed', 0)} passed, {c.get('failed', 0)} failed, {c.get('skipped', 0)} skipped "
                        f"({data.get('collected')} collected).")
        with db() as s:
            failures = [{"time": fmt_dt(e.timestamp), "event": e.event_type, "outcome": e.outcome,
                         "details": ", ".join(f"{k}={v}" for k, v in (e.event_metadata or {}).items())[:160]}
                        for e in recent_audit_events(s, 200) if e.outcome != "success"][:20]
            runs = [{"run": r.id, "type": r.workflow_type, "started": fmt_dt(r.started_at), "error": r.error_category}
                    for r in s.scalars(select(WorkflowRun).where(WorkflowRun.state == "FAILED")
                                       .order_by(WorkflowRun.started_at.desc()).limit(15))]
        st.markdown("**Recent blocked or failed actions**")
        st.dataframe(pd.DataFrame(failures), hide_index=True, width="stretch") if failures else st.caption("None.")
        st.markdown("**Recent failed AI/workflow runs**")
        st.dataframe(pd.DataFrame(runs), hide_index=True, width="stretch") if runs else st.caption("None.")


def _account_section() -> None:
    from src.auth import ROLE_LABELS, ROLE_PERMISSIONS, change_password

    p = ensure_principal()
    st.markdown("### Your account")
    kv_panel([("Name", p.display_name), ("Username", p.username), ("Role", ROLE_LABELS.get(p.role, "") if p.role else "System"),
              ("Can", ", ".join(sorted(x.value.replace("_", " ") for x in ROLE_PERMISSIONS[p.role])) if p.role else "everything")])
    with st.expander("Change password"), st.form("change_pw", clear_on_submit=True):
        current = st.text_input("Current password", type="password", autocomplete="current-password")
        new = st.text_input("New password (8+ characters)", type="password", autocomplete="new-password")
        again = st.text_input("Repeat new password", type="password", autocomplete="new-password")
        if st.form_submit_button("Update password"):
            if new != again:
                st.error("The new passwords don't match.")
            else:
                try:
                    with db() as s:
                        change_password(s, p.user_id or "", current, new)
                    st.success("Password updated.")
                except Exception as exc:  # noqa: BLE001
                    show_exception(exc)
    st.session_state["fx_pref"] = st.toggle(
        "Futuristic effects (animations, glow)", key="fx_toggle", value=st.session_state.get("fx_pref", True),
        help="Purely visual. Animations are also switched off automatically if your system asks to reduce motion.")
    if st.button("Sign out", key="settings_sign_out"):
        sign_out()


def _users_section() -> None:
    from src.auth import ROLE_LABELS, STAFF_ROLES, Role, add_staff_account, list_users, set_user_active

    st.markdown("### Team accounts")
    me = ensure_principal()
    with db() as s:
        users = [(u.id, u.username, u.display_name, Role(u.role), u.is_active, u.last_login_at) for u in list_users(s)]
    st.dataframe(pd.DataFrame([{"Username": u[1], "Name": u[2], "Role": ROLE_LABELS.get(u[3], u[3]),
                                "Active": "yes" if u[4] else "no", "Last sign-in": fmt_dt(u[5])} for u in users]),
                 hide_index=True, width="stretch")
    with st.expander("Add a team member"), st.form("add_user", clear_on_submit=True):
        a, b = st.columns(2)
        username = a.text_input("Username (3+ characters, lowercase)")
        name = b.text_input("Display name")
        a, b = st.columns(2)
        password = a.text_input("Temporary password (8+ characters)", type="password", autocomplete="new-password")
        role = b.selectbox("Role", list(STAFF_ROLES), index=1, format_func=lambda r: ROLE_LABELS[r])
        st.caption("Team members can work on inquiries, quotes, customers and emails, and use the assistant. They can't "
                   "see revenue, Reports, bookings controls, exports or settings. Share the password privately and ask "
                   "them to change it in Settings.")
        if st.form_submit_button("Create account"):
            try:
                with db() as s:
                    add_staff_account(s, username.strip().lower(), password, role, name.strip() or None)
                st.success(f"Account “{username.strip().lower()}” created.")
            except Exception as exc:  # noqa: BLE001
                show_exception(exc)
    others = [u for u in users if u[0] != me.user_id]
    if others:
        with st.expander("Switch an account on or off"):
            pick = st.selectbox("Account", [u[0] for u in others], format_func=lambda i: next(
                f"{u[1]} ({'active' if u[4] else 'switched off'})" for u in others if u[0] == i))
            target = next(u for u in others if u[0] == pick)
            if st.button("Switch off" if target[4] else "Switch on", key="toggle_user"):
                try:
                    with db() as s:
                        set_user_active(s, target[0], not target[4])
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    show_exception(exc)
    st.caption("Accounts can also be managed with `python scripts/manage_users.py`. Passwords are stored only as hashes.")


def render() -> None:
    page_header("Settings", "Your account, the AI assistant, your data and system health.", show_demo_note=False)
    _account_section()
    if has_permission(Permission.MANAGE_SETTINGS):
        st.divider()
        _ai_section()
        st.divider()
        _users_section()
    if has_permission(Permission.ADMIN_DATA):
        st.divider()
        _data_section()
    if has_permission(Permission.VIEW_DIAGNOSTICS):
        st.divider()
        _health_section()
