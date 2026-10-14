"""Shared UI: settings/DB access, design system (CSS), navigation helpers, formatting and small components.

Business logic lives in ``src.services`` / ``src.workflows``; pages only collect input and display results.
"""

from __future__ import annotations

import html
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any

import plotly.graph_objects as go
import streamlit as st
from sqlalchemy.orm import Session

from src.ai.client import OpenAIClient, build_ai_client
from src.auth import (
    SESSION_IDLE_MINUTES,
    Permission,
    Principal,
    current_principal,
    load_principal,
    set_principal,
)
from src.config import APP_NAME, DISCLAIMER, Settings, get_settings
from src.database import init_db, session_scope
from src.domain import INQUIRY_STATUS_LABELS, QUOTE_STATUS_LABELS
from src.exceptions import AINotConfigured
from src.logging_config import configure_logging

# Reference data-viz palette (validated categorical order; single hue for single-series charts).
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
SINGLE = SERIES[0]
INK = "#0B1220"
INK_2 = "#3F4A5C"
MUTED = "#6B7280"
GRID = "#E5E7EB"

_TONES = {  # (background, text)
    "neutral": ("#F1F3F6", "#3F4A5C"), "info": ("#E8F1FD", "#1D4F91"), "warn": ("#FDF3E1", "#8A5300"),
    "good": ("#E6F4EA", "#1E6B34"), "bad": ("#FDECEC", "#9B1C1C"), "accent": ("#E3F2F0", "#0F5F58"),
}
STATUS_TONE = {
    "new": "info", "received": "info", "draft": "neutral", "open": "info", "pending": "warn",
    "needs_information": "warn", "pending_approval": "warn", "AWAITING_APPROVAL": "warn", "NEEDS_INFORMATION": "warn",
    "presented": "accent", "quoted": "accent", "qualified": "accent", "scheduled": "accent", "approved": "accent",
    "APPROVED": "accent", "RECEIVED": "info", "VALIDATING": "info", "READY_FOR_REVIEW": "info",
    "booked": "good", "confirmed": "good", "accepted": "good", "completed": "good", "COMPLETED": "good", "executed": "good",
    "cancelled": "bad", "FAILED": "bad", "failed": "bad", "rejected": "bad", "REJECTED": "bad", "CANCELLED": "neutral",
    "declined": "neutral", "expired": "neutral", "withdrawn": "neutral", "closed": "neutral", "dismissed": "neutral",
    "irrelevant": "neutral", "stated": "good", "inferred": "warn", "missing": "bad",
}
FOLLOWUP_STATUS_LABELS = {"draft": "Draft", "pending_approval": "Waiting for approval", "approved": "Approved",
                          "scheduled": "Scheduled", "completed": "Done", "cancelled": "Cancelled"}
SOURCE_STYLES = {"model": ("#F3E8FF", "#6B21A8", "Model output"), "application": ("#F1F3F6", "#334155", "Application logic"),
                 "tool": ("#E8F1FD", "#1D4F91", "Tool result"), "human": ("#E6F4EA", "#1E6B34", "Human decision")}

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Inter+Tight:wght@600;700&display=swap');
:root { --navy:#0B1B33; --navy-2:#13294B; --ink:#0E1A2B; --ink2:#44506A; --muted:#5B6478; --canvas:#FAF8F4;
        --surface:#FFFFFF; --line:#E8E3D9; --line-2:#EFEBE4; --accent:#2F6BDB; --accent-soft:#EAF1FD; --gold:#B08A4E; }
html, body, [class*="css"], .stMarkdown, button, input, textarea, select { font-family:"Inter","Segoe UI",system-ui,sans-serif; }
.stApp { background: var(--canvas); }
.block-container { padding-top: 1.6rem; padding-bottom: 3rem; max-width: 1240px; }
h1, h2, h3, h4 { font-family:"Inter Tight","Inter",sans-serif !important; color: var(--ink); }
h1 { font-size: 1.75rem !important; font-weight: 700 !important; letter-spacing: -0.025em; margin-bottom: 0 !important; }
h2 { font-size: 1.15rem !important; font-weight: 650 !important; letter-spacing: -0.015em; }
h3, h4 { font-size: 1.02rem !important; font-weight: 650 !important; letter-spacing: -0.01em; }
.fd-sub { color: var(--muted); font-size: 0.94rem; margin: 0.25rem 0 1rem 0; }
/* focus visibility for keyboard users */
*:focus-visible { outline: 2px solid var(--accent) !important; outline-offset: 2px !important; border-radius: 6px; }
/* cards */
.fd-card { background: var(--surface); border:1px solid var(--line); border-radius:14px; padding:16px 18px;
           box-shadow: 0 1px 2px rgba(11,27,51,0.04); height:100%; }
.fd-card .lbl { font-size:0.78rem; color: var(--muted); font-weight:600; letter-spacing:0.01em; }
.fd-card .val { font-family:"Inter Tight","Inter",sans-serif; font-size:1.7rem; font-weight:700; color: var(--ink);
                line-height:1.2; margin-top:6px; font-variant-numeric: tabular-nums; letter-spacing:-0.02em; }
.fd-card .note { font-size:0.78rem; color: var(--muted); margin-top:4px; }
.fd-badge { display:inline-block; padding:2px 9px; border-radius:999px; font-size:0.74rem; font-weight:600; white-space:nowrap; }
.fd-panel { background: var(--surface); border:1px solid var(--line); border-radius:14px; padding:14px 18px; margin-bottom:10px; }
.fd-kv { display:grid; grid-template-columns: 160px 1fr; row-gap:7px; font-size:0.9rem; }
.fd-kv .k { color: var(--muted); } .fd-kv .v { color: var(--ink); }
.fd-empty { border:1px dashed #D9D2C4; border-radius:14px; padding:24px; text-align:center; color: var(--ink2); background:#FFFDF9; }
.fd-empty b { color: var(--ink); display:block; margin-bottom:4px; font-size:1rem; }
.fd-hero { background: var(--navy); color:#fff; border-radius:18px; padding:30px 32px; position:relative; overflow:hidden; }
.fd-hero:after { content:""; position:absolute; right:-60px; top:-60px; width:260px; height:260px; border-radius:50%;
                 border:1px solid rgba(255,255,255,0.08); box-shadow: 0 0 0 40px rgba(255,255,255,0.03); }
.fd-hero h2 { color:#fff !important; font-size:1.45rem !important; margin:0 0 6px 0; }
.fd-hero p { color:#C9D4E8; margin:0; font-size:0.98rem; }
.fd-step { background: var(--surface); border:1px solid var(--line); border-radius:14px; padding:16px; height:100%; }
.fd-step .n { display:inline-block; width:24px; height:24px; border-radius:50%; background: var(--accent-soft); color: var(--accent);
              text-align:center; font-weight:700; font-size:0.8rem; line-height:24px; margin-bottom:6px; }
.fd-item { padding:10px 2px; border-bottom:1px solid var(--line-2); }
.fd-item:last-child { border-bottom:none; }
.fd-item .t { font-weight:600; color: var(--ink); font-size:0.92rem; }
.fd-item .d { color: var(--muted); font-size:0.84rem; margin-top:1px; }
.fd-summary { background: var(--surface); border:1px solid var(--line); border-left:3px solid var(--accent);
              border-radius:14px; padding:14px 18px; }
.fd-summary .h { font-size:0.74rem; font-weight:700; color: var(--accent); text-transform:uppercase; letter-spacing:0.06em; }
.fd-summary p { margin:6px 0 0 0; color: var(--ink); font-size:0.96rem; line-height:1.55; }
.fd-event { border-left:3px solid var(--line); padding:4px 0 6px 12px; margin-left:4px; }
.fd-event .meta { font-size:0.75rem; color: var(--muted); }
.fd-demo { font-size:0.76rem; color:#7A5A1E; background:#F6EEDF; border-radius:8px; padding:5px 10px; display:inline-block; margin-bottom:10px; }
div[data-testid="stMetricValue"] { font-size: 1.35rem; }
div[data-testid="stDataFrame"] { border:1px solid var(--line); border-radius:12px; overflow:hidden; }
div[data-testid="stVerticalBlockBorderWrapper"] { border-color: var(--line) !important; border-radius:14px !important; background: var(--surface); }
/* sidebar: navy chrome, dimmed relative to content */
section[data-testid="stSidebar"] { background: var(--navy); border-right: none; }
section[data-testid="stSidebar"] * { color: #D5DDEB; }
section[data-testid="stSidebar"] a[data-testid="stSidebarNavLink"] { border-radius:8px; }
section[data-testid="stSidebar"] a[data-testid="stSidebarNavLink"]:hover { background: rgba(255,255,255,0.06); }
section[data-testid="stSidebar"] a[aria-current="page"], section[data-testid="stSidebar"] a[data-testid="stSidebarNavLink"][aria-current="page"] {
    background: rgba(127,168,245,0.16) !important; }
section[data-testid="stSidebar"] a[aria-current="page"] span { color:#FFFFFF !important; font-weight:600; }
section[data-testid="stSidebar"] header, section[data-testid="stSidebar"] [data-testid="stSidebarNavSeparator"] { color:#8FA0BD; }
section[data-testid="stSidebar"] button { background: rgba(255,255,255,0.06) !important; border:1px solid rgba(255,255,255,0.14) !important; }
section[data-testid="stSidebar"] button:hover { background: rgba(255,255,255,0.12) !important; }
section[data-testid="stSidebar"] .stCaption, section[data-testid="stSidebar"] small { color:#8FA0BD !important; }
.fd-brand { font-family:"Inter Tight","Inter",sans-serif; font-weight:700; font-size:1.12rem; color:#FFFFFF !important; letter-spacing:-0.01em; }
.fd-brand-sub { font-size:0.78rem; color:#8FA0BD !important; margin-top:2px; }
.fd-user { background: rgba(255,255,255,0.05); border:1px solid rgba(255,255,255,0.10); border-radius:12px; padding:10px 12px; }
.fd-user .n { color:#FFFFFF !important; font-weight:600; font-size:0.9rem; }
.fd-user .r { color:#8FA0BD !important; font-size:0.78rem; }
/* login */
.fd-login-brand { background: var(--navy); border-radius:20px; padding:44px 40px; min-height:460px; color:#fff; position:relative; overflow:hidden; }
.fd-login-brand h1 { color:#fff !important; font-size:2rem !important; margin:18px 0 8px 0 !important; }
.fd-login-brand p { color:#C9D4E8; font-size:1rem; line-height:1.55; }
.fd-login-brand .mark { font-size:1.1rem; font-weight:700; letter-spacing:0.08em; color:#C9D4E8; }
.fd-login-brand:after { content:""; position:absolute; right:-90px; bottom:-90px; width:320px; height:320px; border-radius:50%;
                        border:1px solid rgba(255,255,255,0.10); box-shadow: 0 0 0 50px rgba(255,255,255,0.03), 0 0 0 110px rgba(255,255,255,0.02); }
.fd-login-points { margin-top:28px; } .fd-login-points div { color:#E7ECF5; font-size:0.92rem; margin:8px 0; }
</style>
"""


# ---------------------------------------------------------------------------- settings / db / ai
@st.cache_resource(show_spinner=False)
def _bootstrap() -> Settings:
    from src.auth import SYSTEM, as_principal, bootstrap_demo_users

    configure_logging()
    settings = get_settings()
    init_db(settings.database_url)
    with session_scope(settings.database_url) as s, as_principal(SYSTEM):
        bootstrap_demo_users(s)
    return settings


def app_settings() -> Settings:
    return _bootstrap()


@contextmanager
def db() -> Iterator[Session]:
    with session_scope(app_settings().database_url) as s:
        yield s


def live_client(model: str | None = None) -> tuple[OpenAIClient | None, str]:
    """Return (client, reason). ``client`` is None when live AI is unavailable; reason explains why in plain words."""
    settings = app_settings()
    try:
        client = build_ai_client(settings)
    except AINotConfigured as exc:
        return None, str(exc)
    if model:
        client.model = model
    return client, f"AI is on ({client.model})"


def ai_status() -> tuple[bool, str]:
    """(ready, plain-language status) for the business-facing UI."""
    s = app_settings()
    if s.live_ai_available:
        return True, f"AI assistant is on · {s.openai_model}"
    if not s.ai_key_configured:
        return False, "AI assistant isn't set up yet — add your OpenAI key in Settings."
    return False, "AI assistant is switched off (demo mode)."


# ---------------------------------------------------------------------------- session / authorization
def resolve_session() -> Principal | None:
    """Return the signed-in principal for this browser session (or None), enforcing the idle timeout.
    The principal is re-read from the database on every run, so role changes and deactivation apply at once."""
    auth = st.session_state.get("auth")
    if not auth:
        return None
    now = datetime.now(UTC)
    if now - auth["last_seen"] > timedelta(minutes=SESSION_IDLE_MINUTES):
        st.session_state.clear()
        st.session_state["login_notice"] = "You were signed out after a period of inactivity."
        return None
    auth["last_seen"] = now
    with session_scope(app_settings().database_url) as s:
        principal = load_principal(s, auth["user_id"])
    if principal is None:
        st.session_state.clear()
        return None
    return principal


def ensure_principal() -> Principal:
    """Make sure this run has a signed-in principal; otherwise ask the user to sign in and stop."""
    principal = current_principal()
    if principal is None:
        principal = resolve_session()
        if principal is not None:
            set_principal(principal)
    if principal is None:
        st.markdown(CSS, unsafe_allow_html=True)
        st.info("Please sign in to continue.")
        st.stop()
    return principal


def require_page(permission: Permission) -> Principal:
    principal = ensure_principal()
    if not principal.can(permission):
        st.markdown(CSS, unsafe_allow_html=True)
        st.title("Not available")
        st.warning("Your account doesn't have access to this page. Ask the business owner if you need it.")
        st.stop()
    return principal


def sign_out() -> None:
    p = current_principal()
    if p is not None and p.user_id:
        from src.services.audit_service import record_audit

        with session_scope(app_settings().database_url) as s:
            record_audit(s, "logout", actor="advisor", entity_type="user", entity_id=p.user_id)
    set_principal(None)
    st.session_state.clear()
    st.session_state["login_notice"] = "You've signed out."
    st.rerun()


# ---------------------------------------------------------------------------- navigation
def goto(page_key: str, **state: Any) -> None:
    """Switch to another page, handing over selections. The target page applies them before drawing widgets
    (writing widget-bound keys directly would fail if the widget already exists on the current page)."""
    st.session_state["_handoff"] = state
    pages = st.session_state.get("_pages") or {}
    if page_key in pages:
        st.switch_page(pages[page_key])
    else:  # e.g. in tests without navigation: apply on the next run of this page
        st.rerun()


def apply_handoff() -> None:
    handoff = st.session_state.pop("_handoff", None)
    if handoff:
        for k, v in handoff.items():
            st.session_state[k] = v


def page_link(page_key: str, label: str, icon: str | None = None) -> None:
    pages = st.session_state.get("_pages") or {}
    if page_key in pages:
        st.page_link(pages[page_key], label=label, icon=icon)


# ---------------------------------------------------------------------------- layout
def page_header(title: str, subtitle: str, show_demo_note: bool = True,
                permission: Permission = Permission.VIEW_OPERATIONS) -> None:
    require_page(permission)
    apply_handoff()
    st.markdown(CSS, unsafe_allow_html=True)
    from src.ui.fx import app_fx

    app_fx()
    st.title(title)
    st.markdown(f'<div class="fd-sub">{html.escape(subtitle)}</div>', unsafe_allow_html=True)
    if show_demo_note:
        st.markdown('<div class="fd-demo">Demo workspace · customers, quotes and bookings are fictional · '
                    'no emails are sent and no real bookings are made</div>', unsafe_allow_html=True)


def sidebar_brand() -> None:
    from src.auth import ROLE_LABELS

    ready, _ = ai_status()
    principal = current_principal()
    with st.sidebar:
        st.markdown(f'<div class="fd-brand">✈ {APP_NAME}<span class="pulse" title="Online"></span></div>'
                    '<div class="fd-brand-sub">Private aviation operations desk</div>',
                    unsafe_allow_html=True)
        st.markdown(" ")
        if principal is not None and not principal.is_system:
            role = ROLE_LABELS.get(principal.role, "") if principal.role else ""
            st.markdown(f'<div class="fd-user"><div class="n">{html.escape(principal.display_name)} · '
                        f'{html.escape(principal.username)}</div><div class="r">{role} · AI assistant '
                        f'{"on" if ready else "off"}</div></div>', unsafe_allow_html=True)
            st.markdown(" ")
            if principal.can(Permission.USE_ASSISTANT):
                with st.popover("✨ Ask the assistant", width="stretch"):
                    q = st.text_input("Your question", key="sidebar_q", placeholder="Which inquiries need follow-up?")
                    if st.button("Ask", key="sidebar_ask", type="primary", disabled=not q.strip()):
                        goto("assistant", chat_pending=q.strip())
            if st.button("Sign out", key="sign_out", width="stretch"):
                sign_out()
        st.caption(DISCLAIMER)


def badge(text: str, tone_or_status: str | None = None) -> str:
    tone = tone_or_status if tone_or_status in _TONES else STATUS_TONE.get(tone_or_status or text, "neutral")
    bg, fg = _TONES[tone]
    return f'<span class="fd-badge" style="background:{bg};color:{fg}">{html.escape(str(text))}</span>'


def status_badge(status: str, kind: str = "inquiry") -> str:
    labels = {"inquiry": INQUIRY_STATUS_LABELS, "quote": QUOTE_STATUS_LABELS, "followup": FOLLOWUP_STATUS_LABELS}.get(kind, {})
    return badge(labels.get(status, status.replace("_", " ").capitalize()), status)


def source_badge(source: str) -> str:
    bg, fg, label = SOURCE_STYLES.get(source, ("#F1F3F6", "#334155", source))
    return f'<span class="fd-badge" style="background:{bg};color:{fg}">{label}</span>'


def metric_card(label: str, value: str, note: str = "", help_text: str | None = None) -> None:
    title = f' title="{html.escape(help_text)}"' if help_text else ""
    st.markdown(f'<div class="fd-card"{title}><div class="lbl">{html.escape(label)}</div>'
                f'<div class="val">{html.escape(value)}</div><div class="note">{html.escape(note)}</div></div>',
                unsafe_allow_html=True)


def metric_row(items: list[tuple[str, str, str, str | None]]) -> None:
    cols = st.columns(len(items))
    for col, (label, value, note, help_text) in zip(cols, items, strict=True):
        with col:
            metric_card(label, value, note, help_text)


def empty_state(title: str, body: str) -> None:
    st.markdown(f'<div class="fd-empty"><b>{html.escape(title)}</b>{html.escape(body)}</div>', unsafe_allow_html=True)


def kv_panel(rows: list[tuple[str, Any]]) -> None:
    inner = "".join(f'<div class="k">{html.escape(k)}</div><div class="v">{v if str(v).startswith("<span") else html.escape(str(v))}</div>'
                    for k, v in rows)
    st.markdown(f'<div class="fd-panel"><div class="fd-kv">{inner}</div></div>', unsafe_allow_html=True)


def item_row(title: str, detail: str) -> None:
    st.markdown(f'<div class="fd-item"><div class="t">{html.escape(title)}</div><div class="d">{html.escape(detail)}</div></div>',
                unsafe_allow_html=True)


# ---------------------------------------------------------------------------- formatting
def fmt_money(amount: float | None, currency: str | None) -> str:
    if amount is None:
        return "—"
    return f"{amount:,.0f} {currency or ''}".strip()


def fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def fmt_dt(value: datetime | None) -> str:
    return value.strftime("%d %b %Y, %H:%M UTC") if value else "—"


def fmt_date(value: date | None) -> str:
    return value.strftime("%d %b %Y") if value else "—"


def when(inq: Any) -> str:
    if inq.departure_date:
        return fmt_date(inq.departure_date)
    if inq.departure_earliest and inq.departure_latest:
        return f"{inq.departure_earliest:%d %b} – {inq.departure_latest:%d %b %Y}"
    return "date not set"


def inquiry_label(inq: Any) -> str:
    route = f"{inq.origin or '?'} → {inq.destination or '?'}"
    status = INQUIRY_STATUS_LABELS.get(inq.status, inq.status)
    return f"{inq.customer_label} · {route} · {when(inq)} · {status}"


def style_figure(fig: go.Figure, height: int = 300, title: str | None = None) -> go.Figure:
    """Recessive axes/grid, thin marks, consistent typography, hover enabled."""
    fig.update_layout(
        height=height, margin=dict(l=8, r=8, t=36 if title else 10, b=8), title=dict(text=title or "", font=dict(size=13, color=INK)),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Inter, Segoe UI, sans-serif", size=12, color=INK_2),
        hoverlabel=dict(bgcolor="#FFFFFF", font_color=INK, bordercolor=GRID), showlegend=False, bargap=0.35,
    )
    fig.update_xaxes(showgrid=False, linecolor=GRID, tickfont=dict(color=MUTED))
    fig.update_yaxes(gridcolor="#EEF0F3", zeroline=False, linecolor=GRID, tickfont=dict(color=MUTED))
    return fig


def show_exception(exc: Exception) -> None:
    """User-safe error display (no stack traces, no secrets)."""
    import logging

    from src.exceptions import FlightDeskError
    from src.security import redact_secrets

    if isinstance(exc, FlightDeskError):
        st.error(redact_secrets(str(exc)))
    else:
        st.error("Something went wrong and nothing was changed. Please try again; details were written to the log.")
        logging.getLogger("src.ui").exception("UI action failed")
