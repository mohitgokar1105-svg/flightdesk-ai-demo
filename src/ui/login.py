"""Sign-in screen: an animated "operations system" scene on the left, a glass sign-in panel on the right."""

from __future__ import annotations

import os
from datetime import UTC, datetime

import streamlit as st
from sqlalchemy import select

from src.auth import SYSTEM, as_principal, authenticate
from src.config import APP_NAME
from src.models import EvaluationRun
from src.places import all_places, resolve_place
from src.ui.common import CSS, app_settings, db, page_link
from src.ui.fx import login_scene


def _ticker() -> list[str]:
    """Real facts only — nothing invented for effect."""
    settings = app_settings()
    items = [f"{len(all_places()):,} airports indexed",
             f"AI core: {settings.openai_model}" if settings.live_ai_available else "AI core: standby (records-only answers)",
             "Passwords scrypt-hashed", "Every AI action needs human approval", "Nothing is ever sent without you"]
    try:
        with db() as s, as_principal(SYSTEM):
            run = s.scalar(select(EvaluationRun).where(EvaluationRun.approach == "assistant_chat")
                           .order_by(EvaluationRun.created_at.desc()).limit(1))
            if run is not None and run.metrics and run.metrics.get("n_cases", 0) >= 100:
                rate = (run.metrics.get("pass_rate") or {}).get("value")
                if rate is not None:
                    items.insert(2, f"Assistant eval: {rate:.0%} of {run.metrics['n_cases']} test cases")
    except Exception:  # noqa: BLE001 - the ticker is decoration; never block sign-in
        pass
    return items


def _home_base() -> str | None:
    p = resolve_place(os.getenv("FLIGHTDESK_HOME_AIRPORT", "CLT"))
    if p is None:
        return None
    return f"{p.iata} {abs(p.lat):.2f}°{'N' if p.lat >= 0 else 'S'} {abs(p.lon):.2f}°{'E' if p.lon >= 0 else 'W'}"


def render() -> None:
    st.markdown(CSS, unsafe_allow_html=True)
    app_settings()  # make sure the database and demo accounts are ready
    left, _, right = st.columns([1.25, 0.08, 0.9])
    with left:
        login_scene(APP_NAME, _ticker(), _home_base())
    with right:
        st.markdown("<div style='height:70px'></div>", unsafe_allow_html=True)
        st.markdown('<div class="fx-signin-head">SECURE ACCESS</div><div class="fx-signin-title">Team sign in</div>'
                    '<div class="fx-signin-sub">For the team: use the account your business owner gave you.</div>',
                    unsafe_allow_html=True)
        notice = st.session_state.pop("login_notice", None)
        if notice:
            st.info(notice)
        with st.form("login", clear_on_submit=False):
            username = st.text_input("Username", autocomplete="username")
            password = st.text_input("Password", type="password", autocomplete="current-password")
            submitted = st.form_submit_button("Sign in", type="primary", width="stretch")
        if submitted:
            with db() as s:
                result = authenticate(s, username, password)
            if result.ok and result.principal is not None:
                now = datetime.now(UTC)
                fx = st.session_state.get("fx_pref", True)
                st.session_state.clear()
                st.session_state["auth"] = {"user_id": result.principal.user_id, "login_at": now, "last_seen": now}
                st.session_state["fx_pref"] = fx
                st.session_state["launch_fx"] = True
                st.rerun()
            else:
                st.error(result.message)
        st.caption("Demo workspace with fictional data. Demo accounts are configured locally in the .env file — they are "
                   "not production credentials.")
        st.divider()
        st.markdown("**Are you a customer?** You don't need an account.")
        page_link("request", "Request a flight →", icon=":material/flight_takeoff:")
        page_link("status", "Check an existing request", icon=":material/search:")
