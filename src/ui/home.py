"""Overview: the business owner's at-a-glance view: a few numbers, a plain summary and "Ask AI about your business"."""

from __future__ import annotations

import json

import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import select

from src.ai.prompts import EXPLANATION_INSTRUCTIONS, explanation_input
from src.auth import Permission, has_permission
from src.models import Inquiry, utcnow
from src.schemas import LLMExplanation
from src.seed import seed_demo_data
from src.services.dashboard_service import (
    PERIODS,
    PROFIT_NOTE,
    booking_funnel,
    business_kpis,
    change_text,
    needs_attention,
    plain_summary,
    recent_activity,
    requested_routes,
    resolve_period,
    summary_facts,
    trend_frame,
    trend_window,
)
from src.ui.common import (
    SERIES,
    app_settings,
    apply_handoff,
    db,
    empty_state,
    goto,
    item_row,
    live_client,
    metric_row,
    page_header,
    require_page,
    show_exception,
    style_figure,
)
from src.ui.visuals import demand_map, funnel

PAGE_FOR_KIND = {"followup_overdue": "followups", "followup_today": "followups", "quote_to_review": "quotes",
                 "quote_no_reply": "followups", "missing_details": "inquiries", "new_inquiry": "inquiries",
                 "missing_contact": "customers"}


def _greeting() -> str:
    hour = utcnow().hour
    return "Good morning" if hour < 12 else ("Good afternoon" if hour < 18 else "Good evening")


def _first_run() -> None:
    st.markdown('<div class="fd-hero"><h2>Welcome to FlightDesk</h2><p>Keep every charter inquiry, quote and follow-up in '
                'one place — with an AI assistant that knows your numbers.</p></div>', unsafe_allow_html=True)
    st.markdown(" ")
    cols = st.columns(3)
    steps = [("Add an inquiry", "Paste a customer's email or message. FlightDesk pulls out the route, dates and passengers "
              "and shows you what's missing."),
             ("Record quotes", "Enter the quotes you get from operators and compare them side by side — price, capacity "
              "and what's included."),
             ("Stay on top of follow-ups", "Draft replies in seconds, set due dates, and see what needs attention every morning.")]
    for i, (col, (title, body)) in enumerate(zip(cols, steps, strict=True), start=1):
        col.markdown(f'<div class="fd-step"><div class="n">{i}</div><div style="font-weight:650">{title}</div>'
                     f'<div style="color:#6B7280;font-size:0.88rem;margin-top:4px">{body}</div></div>', unsafe_allow_html=True)
    st.markdown(" ")
    a, b, _ = st.columns([1.2, 1.3, 3])
    if a.button("Add your first inquiry", type="primary"):
        goto("inquiries", inquiries_tab="add")
    if not has_permission(Permission.ADMIN_DATA):
        st.caption("Ask the business owner to load the sample data if you'd like to explore first.")
        return
    if b.button("Explore with sample data"):
        try:
            with st.spinner("Loading fictional sample data…"), db() as s:
                seed_demo_data(s, app_settings())
            st.rerun()
        except Exception as exc:  # noqa: BLE001
            show_exception(exc)


@st.cache_data(ttl=3600, show_spinner=False)
def _ai_summary(facts_json: str, model: str) -> tuple[str | None, str]:
    """AI rephrasing of deterministic facts, cached per fact set. Withheld if it introduces numbers not in the facts."""
    from src.workflows.workflow_engine import unsupported_numbers

    client, _ = live_client()
    if client is None:
        return None, "unavailable"
    facts = json.loads(facts_json)
    instructions = EXPLANATION_INSTRUCTIONS + ("\n- Write for a business owner: 2-3 sentences, plain English, lead with what "
                                               "matters most (e.g. growth, overdue follow-ups). No bullet points.")
    try:
        res = client.parse(instructions=instructions, user_input=explanation_input(facts), schema=LLMExplanation,
                           purpose="dashboard_summary")
    except Exception:  # noqa: BLE001 - shown as the deterministic summary instead, labelled accordingly
        return None, "failed"
    if unsupported_numbers(res.parsed.summary, facts):
        return None, "withheld"
    return res.parsed.summary, "ok"


def _summary(facts: dict) -> None:
    sentences = plain_summary(facts)
    text, label = " ".join(sentences), "Summary · from your records"
    client, _ = live_client()
    if client is not None:
        key = json.dumps(facts, sort_keys=True, default=str)
        ai_text, status = _ai_summary(key, client.model)
        if ai_text:
            text, label = ai_text, "AI summary · based only on your records"
        elif status in ("failed", "withheld"):
            label = "Summary · from your records (AI summary unavailable right now)"
    st.markdown(f'<div class="fd-summary"><div class="h">{label}</div><p>{text}</p></div>', unsafe_allow_html=True)


def _trend(selected) -> None:
    period = trend_window(selected)
    with db() as s:
        df = trend_frame(s, period)
    st.markdown("#### Business performance")
    if df.empty:
        empty_state("No activity in this period", "Try a longer period.")
        return
    granularity = "week" if period.days is not None and period.days <= 92 else "month"
    fig = go.Figure()
    for i, name in enumerate(["Inquiries received", "Quotes sent", "Bookings confirmed"]):
        part = df[df["series"] == name]
        fig.add_scatter(x=part["bucket"], y=part["count"], name=name, mode="lines+markers",
                        line=dict(width=2, color=SERIES[i]), marker=dict(size=8, color=SERIES[i]),
                        hovertemplate=f"{name}<br>{granularity.capitalize()} of %{{x|%d %b %Y}}: %{{y}}<extra></extra>")
    style_figure(fig, height=300)
    fig.update_layout(showlegend=True, legend=dict(orientation="h", y=1.12, x=0, font=dict(size=12)))
    fig.update_yaxes(rangemode="tozero", title=None)
    st.plotly_chart(fig, width="stretch")
    st.caption(f"Counts per {granularity}, {period.describe()}. Quotes sent = quotes marked as sent to the customer.")


def _attention() -> None:
    with db() as s:
        items = needs_attention(s)
    st.markdown("#### Needs attention")
    if not items:
        empty_state("You're all caught up", "Nothing is overdue or waiting on you.")
        return
    for n, item in enumerate(items[:6]):
        c1, c2 = st.columns([3.2, 1])
        with c1:
            item_row(item.title, item.detail)
        if c2.button("Open", key=f"att_{n}"):
            page = PAGE_FOR_KIND.get(item.kind, "inquiries")
            with db() as s:
                inq = s.get(Inquiry, item.inquiry_id) if item.inquiry_id else None
                customer_id = inq.customer_id if inq else None
            goto(page, inquiries_tab="list", selected_inquiry=item.inquiry_id, selected_followup=item.followup_id,
                 selected_customer=customer_id)
    if len(items) > 6:
        st.caption(f"And {len(items) - 6} more — ask the assistant “What should I prioritize today?”")


def _activity() -> None:
    with db() as s:
        events = recent_activity(s, 8)
    st.markdown("#### Recent activity")
    if not events:
        empty_state("No activity yet", "Actions you take — new inquiries, quotes, bookings — will appear here.")
        return
    for e in events:
        item_row(f"{e['text']} — {e['who']}" if e["who"] else e["text"], f"{e['time']:%d %b %Y}")


ASK_EXAMPLES = ["Which inquiries need attention today?", "How many inquiries became confirmed bookings this month?",
                "Which quotes are awaiting a customer response?", "Summarize our outstanding opportunities.",
                "Draft a follow-up for a customer who hasn't replied to our quote."]
MONEY_TERMS = [
    ("Customer budget", "What the customer said they'd like to spend. Only recorded if they mention it."),
    ("Estimated opportunity", "Your own estimate of what a deal might be worth. A guess for planning — not money earned."),
    ("Quoted value", "The cheapest live all-in quote for each inquiry (alternatives aren't added up). An offer, not a sale."),
    ("Confirmed booking value", "What customers have confirmed bookings for (confirmed + completed; cancelled excluded). "
                                "The closest thing to sales in FlightDesk."),
    ("Revenue", "Not tracked: FlightDesk doesn't record payments or invoices, so booking value isn't the same as money received."),
    ("Profit", "Not calculated: operator costs and commissions aren't recorded."),
]


def _ask_ai() -> None:
    st.markdown("#### ✨ Ask AI about your business")
    with st.container(border=True):
        q = st.text_input("Your question", key="home_ask", placeholder="e.g. Which inquiries need attention today?",
                          label_visibility="collapsed")
        if st.button("Ask", key="home_ask_go", type="primary", disabled=not (q or "").strip()):
            goto("assistant", chat_pending=q.strip())
        st.caption("Or try:")
        for i, example in enumerate(ASK_EXAMPLES):
            if st.button(example, key=f"home_ex_{i}", width="stretch"):
                goto("assistant", chat_pending=example)
        st.caption("Answers come from your records through approved tools. Anything that would change a record needs your OK.")


def _visuals(period) -> None:
    with db() as s:
        routes = requested_routes(s, period)
        stages = booking_funnel(s, period)
    left, right = st.columns([1.55, 1])
    with left:
        st.markdown("#### Where customers want to fly")
        if not routes:
            empty_state("No requests in this period", "Routes appear here as inquiries come in.")
        else:
            used = demand_map(routes, key="home_demand_map")
            if used["mapped"]:
                st.caption(f"{used['mapped']} of {len(routes)} inquiries {period.label.lower()} drawn by route — thicker "
                           "lines are asked for more often."
                           + (f" {used['unplaced']} couldn't be placed (place not recognised or not given)."
                              if used["unplaced"] else ""))
            else:
                empty_state("No routes to draw yet", "Use the airport suggestions in From / To so trips can be mapped.")
    with right:
        st.markdown("#### From request to booking")
        funnel(stages, key="home_funnel")


def _money_terms() -> None:
    with st.expander("What do the money terms mean?"):
        for term, meaning in MONEY_TERMS:
            st.markdown(f"**{term}** — {meaning}")
        st.caption("Different currencies are never added together.")


def render() -> None:
    require_page(Permission.VIEW_OPERATIONS)
    apply_handoff()
    with db() as s:
        has_data = s.scalar(select(Inquiry.id).limit(1)) is not None
    if not has_data:
        page_header(f"{_greeting()} 👋", "Let's get your first inquiry in.")
        _first_run()
        return

    head, picker = st.columns([3, 1.4])
    with head:
        page_header(f"{_greeting()} 👋", "Here's how the business is doing.")
    with picker:
        st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)
        key = st.selectbox("Reporting period", list(PERIODS), index=list(PERIODS).index("this_month"),
                           format_func=lambda k: PERIODS[k], key="home_period")
    period = resolve_period(key)
    with db() as s:
        k = business_kpis(s, period)
        facts = summary_facts(s, period)
    st.caption(f"Reporting period: {period.label} ({period.describe()})")
    prev = k.previous
    prev_label = k.previous_label.split(" (")[0] or "previous period"
    if k.booked_value:
        main_ccy = max(k.booked_value, key=lambda c: k.booked_value[c])
        value = f"{k.booked_value[main_ccy]:,.0f} {main_ccy}"
        others = [f"{v:,.0f} {c}" for c, v in k.booked_value.items() if c != main_ccy]
        value_note = ("+ " + ", ".join(others)) if others else "confirmed & completed, excl. cancelled"
    else:
        value, value_note = "—", "no confirmed bookings in this period"
    if k.financials_visible:
        last_card = ("Confirmed booking value", value, value_note,
                     "Sum of confirmed and completed bookings in the period, per currency. Not revenue received, not profit.")
    else:
        last_card = ("Overdue follow-ups", f"{k.followups_overdue}", "past their due date",
                     "Open follow-ups whose due date has passed.")
    metric_row([
        ("New inquiries", f"{k.new_inquiries}", change_text(k.new_inquiries, prev.get("new_inquiries"), prev_label),
         "Inquiries received in the reporting period."),
        ("Quotes in progress", f"{k.quotes_in_progress}", f"{k.quotes_sent} sent in this period",
         "Right now: quotes received from operators or sent to customers on open inquiries, awaiting a decision."),
        ("Confirmed bookings", f"{k.confirmed_bookings}", change_text(k.confirmed_bookings, prev.get("confirmed_bookings"), prev_label),
         "Bookings confirmed in the period (confirmed or completed; cancelled bookings excluded)."),
        ("Follow-ups needing attention", f"{k.followups_due}",
         f"{k.followups_overdue} overdue" if k.followups_overdue else "none overdue",
         "Open follow-ups due within the next 7 days, including overdue ones (current workload)."),
        last_card,
    ])
    st.markdown(" ")
    try:
        _summary(facts)
    except Exception as exc:  # noqa: BLE001
        show_exception(exc)
    st.markdown(" ")
    try:
        _visuals(period)
    except Exception as exc:  # noqa: BLE001
        show_exception(exc)
    st.markdown(" ")
    left, right = st.columns([1.35, 1])
    with left:
        _attention()
        st.markdown(" ")
        _activity()
    with right:
        if has_permission(Permission.USE_ASSISTANT):
            _ask_ai()
    st.markdown(" ")
    with st.expander("📈 Trend over the last 12 weeks"):
        _trend(period)
    if k.financials_visible:
        _money_terms()
        st.caption(PROFIT_NOTE + " Different currencies are never added together.")
