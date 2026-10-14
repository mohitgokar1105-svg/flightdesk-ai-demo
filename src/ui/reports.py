"""Reports: detailed, filterable business analytics with definitions, record-level drill-downs and exports."""

from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from src.auth import Permission
from src.domain import INQUIRY_STATUS_LABELS, InquiryStatus, ServiceCategory
from src.services.analytics_service import (
    METRIC_DEFINITIONS,
    DashboardFilters,
    chart_data,
    compute_metrics,
    drilldown,
)
from src.services.csv_service import EXPORTABLE, export_entity, frame_to_csv_bytes
from src.services.dashboard_service import PERIODS, PROFIT_NOTE, resolve_period
from src.ui.common import SINGLE, db, empty_state, fmt_money, fmt_pct, metric_row, page_header, style_figure

DRILLDOWNS = {
    "Inquiries received": "total_inquiries", "Open inquiries": "open_inquiries", "Quotes recorded": "quotes_created",
    "Quotes awaiting a decision": "quotes_awaiting_decision", "Confirmed bookings": "confirmed_bookings",
    "Completed trips": "completed_bookings", "Cancelled bookings": "cancelled_bookings",
    "Booked value (records)": "confirmed_booking_value", "Converted inquiries": "conversion_rate",
    "Open follow-ups": "outstanding_followups", "Overdue follow-ups": "overdue_followups",
}


def _money_block(title: str, values: dict[str, float], note: str) -> None:
    st.markdown(f"**{title}**")
    if not values:
        st.caption("None in this period.")
    for ccy, v in values.items():
        st.markdown(f"<div style='font-size:1.25rem;font-weight:700'>{fmt_money(v, ccy)}</div>", unsafe_allow_html=True)
    st.caption(note)


def render() -> None:
    page_header("Reports", "The numbers behind the business, with exactly how each one is counted.",
                permission=Permission.VIEW_FINANCIALS)
    a, b, c = st.columns([1.2, 1.6, 1.6])
    key = a.selectbox("Period", list(PERIODS), index=list(PERIODS).index("this_quarter"),
                      format_func=lambda k: PERIODS[k], key="rep_period")
    period = resolve_period(key or "this_quarter")
    statuses = b.multiselect("Inquiry status", [s.value for s in InquiryStatus],
                             format_func=lambda v: INQUIRY_STATUS_LABELS.get(v, v),
                             placeholder="All statuses")
    services = c.multiselect("Service", [s.value for s in ServiceCategory], placeholder="All services",
                             format_func=lambda v: v.replace("_", " ").capitalize())
    sample = st.toggle("Include sample (fictional) records", value=True)
    f = DashboardFilters(start=period.start, end=period.end, inquiry_statuses=statuses or None,
                         service_categories=services or None, include_sample=sample)
    st.caption(f"{period.label}: {period.describe()}")
    with db() as s:
        m = compute_metrics(s, f)
        charts = chart_data(s, f, "USD")
    c_ = m.counts
    r = m.ratios
    conv = r["conversion_rate"]
    metric_row([
        ("Inquiries received", f"{c_['total_inquiries']}", f"{c_['open_inquiries']} still open", METRIC_DEFINITIONS["total_inquiries"]),
        ("Quotes recorded", f"{c_['quotes_created']}", f"{c_['quotes_awaiting_decision']} awaiting a decision",
         METRIC_DEFINITIONS["quotes_created"]),
        ("Bookings", f"{c_['confirmed_bookings'] + c_['completed_bookings']}",
         f"{c_['completed_bookings']} completed · {c_['cancelled_bookings']} cancelled", METRIC_DEFINITIONS["confirmed_bookings"]),
        ("Booking conversion", fmt_pct(conv.value), f"{conv.numerator} of {conv.denominator} inquiries booked",
         METRIC_DEFINITIONS["conversion_rate"]),
        ("Days to confirm (avg)", "n/a" if m.avg_days_to_confirmation is None else f"{m.avg_days_to_confirmation}",
         "from inquiry to confirmed booking", METRIC_DEFINITIONS["avg_days_to_confirmation"]),
    ])
    st.markdown("### Value")
    st.caption("Three different things, never added together: what deals might be worth, what operators quoted, and what was "
               "actually booked. Each is shown per currency.")
    x, y, z = st.columns(3)
    with x:
        _money_block("Pipeline (your estimates)", m.money["estimated_opportunity_value"], "Open inquiries only. Not revenue.")
    with y:
        _money_block("Quoted", m.money["total_quoted_value"], "Cheapest live quote per inquiry, so alternatives aren't double-counted. Not revenue.")
    with z:
        _money_block("Booked", m.money["confirmed_booking_value"], "Confirmed and completed bookings. Cancelled excluded.")
    st.info(PROFIT_NOTE)

    st.markdown("### Pipeline")
    left, right = st.columns(2)
    with left:
        df = charts["funnel"]
        fig = go.Figure(go.Funnel(y=df["stage"], x=df["inquiries"], marker_color=SINGLE, textinfo="value+percent initial",
                                  hovertemplate="%{y}: %{x} inquiries<extra></extra>"))
        st.plotly_chart(style_figure(fig, title="From inquiry to trip (inquiries received in the period)"), width="stretch")
    with right:
        df = charts["status_distribution"].copy()
        if df.empty:
            empty_state("No inquiries", "Nothing received in this period.")
        else:
            df["label"] = df["status"].map(lambda v: INQUIRY_STATUS_LABELS.get(v, v))
            df = df.sort_values("count")
            fig = go.Figure(go.Bar(y=df["label"], x=df["count"], orientation="h", marker_color=SINGLE, marker_cornerradius=4,
                                   hovertemplate="%{y}: %{x}<extra></extra>"))
            st.plotly_chart(style_figure(fig, title="Where inquiries are now"), width="stretch")
    st.markdown("### Follow-up discipline")
    fc = r["followup_completion_rate"]
    ra = r["recommendation_acceptance_rate"]
    metric_row([
        ("Open follow-ups", f"{c_['outstanding_followups']}", f"{c_['overdue_followups']} overdue", METRIC_DEFINITIONS["outstanding_followups"]),
        ("Follow-ups completed", fmt_pct(fc.value), f"{fc.numerator} of {fc.denominator}", METRIC_DEFINITIONS["followup_completion_rate"]),
        ("Suggestions used", fmt_pct(ra.value), f"{ra.numerator} of {ra.denominator} reviewed", METRIC_DEFINITIONS["recommendation_acceptance_rate"]),
    ])

    st.markdown("### See the records")
    choice = st.selectbox("Show the records behind", list(DRILLDOWNS))
    with db() as s:
        detail = drilldown(s, f, DRILLDOWNS[choice])
    if detail.empty:
        empty_state("No matching records", "Try a different period or filter.")
    else:
        st.dataframe(detail, hide_index=True, width="stretch")
        st.download_button("Download as CSV", frame_to_csv_bytes(detail), file_name=f"{DRILLDOWNS[choice]}.csv", mime="text/csv")

    with st.expander("Export all records"):
        cols = st.columns(len(EXPORTABLE))
        for col, entity in zip(cols, EXPORTABLE, strict=True):
            if col.button(entity.capitalize(), key=f"exp_{entity}"):
                with db() as s:
                    path, df = export_entity(s, entity)
                st.session_state["rep_export"] = (path.name, frame_to_csv_bytes(df), len(df))
        if st.session_state.get("rep_export"):
            name, data, n = st.session_state["rep_export"]
            st.success(f"{n} rows saved to data/exports/{name}")
            st.download_button(f"Download {name}", data, file_name=name, mime="text/csv")
    with st.expander("How each number is counted"):
        for k, v in METRIC_DEFINITIONS.items():
            st.markdown(f"**{k.replace('_', ' ').capitalize()}** — {v}")
