"""Quotes: record operator quotes (manual entry), compare them, and record a confirmed booking with your approval."""

from __future__ import annotations

import pandas as pd
import streamlit as st
from pydantic import ValidationError
from sqlalchemy import select

from src.auth import Permission, has_permission
from src.domain import (
    BOOKING_TRANSITIONS,
    QUOTE_TRANSITIONS,
    SUPPORTED_CURRENCIES,
    BookingStatus,
    QuoteStatus,
)
from src.models import ActionProposal, Booking, Inquiry
from src.schemas import QUOTE_LINE_ITEMS, QuoteCreate
from src.services.booking_service import cancel_booking, complete_booking
from src.services.quote_service import (
    SERVICE_LABELS,
    change_quote_status,
    compare_quotes,
    create_quote,
    list_quotes_for_inquiry,
)
from src.ui.common import (
    app_settings,
    badge,
    db,
    empty_state,
    fmt_date,
    fmt_dt,
    fmt_money,
    inquiry_label,
    kv_panel,
    live_client,
    page_header,
    show_exception,
    status_badge,
    when,
)
from src.ui.documents import operator_pdf_reader, quote_pdf_panel
from src.ui.operators import operators_tab, rfq_panel
from src.workflows import workflow_engine as wf

ACTIONS = {QuoteStatus.PRESENTED: "Mark as sent to customer", QuoteStatus.DECLINED: "Customer declined",
           QuoteStatus.EXPIRED: "Expired", QuoteStatus.WITHDRAWN: "Operator withdrew"}


def _add_form(iid: str, default_ccy: str, prefill: dict | None = None) -> None:
    pf = prefill or {}
    ccy = pf.get("currency", default_ccy)
    title = "➕ Add a quote from an operator" + (" — pre-filled from the PDF, please check" if pf else "")
    with st.expander(title, expanded=bool(pf)), st.form(f"q_{iid}", clear_on_submit=True):
        st.caption("Enter the quote exactly as the operator sent it. Demo quotes are fictional.")
        a, b, c = st.columns(3)
        operator = a.text_input("Operator", value=pf.get("operator", ""))
        aircraft = b.text_input("Aircraft type", value=pf.get("aircraft", ""), placeholder="e.g. Midsize jet")
        capacity = c.number_input("Seats", min_value=1, max_value=500, value=pf.get("capacity"), step=1)
        a, b, c = st.columns(3)
        price = a.number_input("Quoted price", min_value=0.0, value=pf.get("price"), step=500.0)
        fees = b.number_input("Additional fees (if any)", min_value=0.0, value=pf.get("fees"), step=100.0,
                              help="Extras the operator lists on top of the price, in the same currency.")
        currency = c.selectbox("Currency", SUPPORTED_CURRENCIES,
                               index=SUPPORTED_CURRENCIES.index(ccy if ccy in SUPPORTED_CURRENCIES else "USD"))
        fees_desc = st.text_input("What are the fees for?", value=pf.get("fees_description", ""),
                                  placeholder="e.g. de-icing, overnight crew")
        a, b = st.columns(2)
        included = a.multiselect("Included", QUOTE_LINE_ITEMS, default=pf.get("included", []),
                                 format_func=lambda x: str(SERVICE_LABELS.get(x, x)).capitalize())
        excluded = b.multiselect("Not included", QUOTE_LINE_ITEMS, default=pf.get("excluded", []),
                                 format_func=lambda x: str(SERVICE_LABELS.get(x, x)).capitalize())
        a, b = st.columns(2)
        restrictions = a.text_input("Conditions / restrictions", value=pf.get("restrictions", ""))
        valid_until = b.date_input("Valid until", value=pf.get("valid_until"), format="DD/MM/YYYY")
        notes = st.text_input("Notes")
        if st.form_submit_button("Save quote", type="primary"):
            try:
                data = QuoteCreate(inquiry_id=iid, operator_label=operator, total_price=price or 0, additional_fees=fees,
                                   fees_description=fees_desc, currency=currency, aircraft_type=aircraft,
                                   passenger_capacity=capacity, included_services=included, excluded_services=excluded,
                                   restrictions=restrictions, valid_until=valid_until, notes=notes,
                                   source_description="Read from operator PDF, checked by advisor" if pf else "Entered manually")
                with db() as s:
                    create_quote(s, data)
                st.session_state.pop(f"opdf_{iid}_result", None)
                st.success("Quote saved.")
            except ValidationError as exc:
                for e in exc.errors():
                    field = str(e["loc"][-1]).replace("_", " ") if e["loc"] else "quote"
                    st.error(f"{field.capitalize()}: {e['msg'].replace('Value error, ', '')}")
            except Exception as exc:  # noqa: BLE001
                show_exception(exc)


def _booking(iid: str, quotes: list) -> None:
    st.markdown("#### Record a confirmed booking")
    if not has_permission(Permission.MANAGE_BOOKINGS):
        st.caption("Bookings are confirmed by the business owner. Let them know this quote has been accepted.")
        return
    st.caption("Use this once the customer and operator have confirmed. FlightDesk checks the details and asks for your OK; "
               "it doesn't contact anyone or take payment.")
    state = st.session_state.setdefault("booking_ui", {})
    pending = state.get(iid)
    if pending:
        with db() as s:
            prop = s.get(ActionProposal, pending["proposal_id"])
            if prop is None or prop.status != "pending":
                state.pop(iid, None)
                pending = None
    if not pending:
        bookable = [q for q in quotes if q.status in ("received", "presented")]
        if not bookable:
            st.caption("No open quotes to book.")
            return
        qid = st.selectbox("Quote", [q.id for q in bookable], key=f"bk_{iid}",
                           format_func=lambda x: next(f"{q.operator_label} · {fmt_money(q.all_in_price, q.currency)}"
                                                      for q in bookable if q.id == x))
        notes = st.text_input("Booking notes", key=f"bkn_{iid}")
        if st.button("Check & continue", key=f"bkb_{iid}"):
            with db() as s:
                res = wf.start_booking_confirmation(s, app_settings(), qid, notes=notes or None)
            if res.ok:
                state[iid] = {"proposal_id": res.data["proposal_id"], "warnings": res.data["warnings"],
                              "value": res.data["booked_value"], "currency": res.data["currency"]}
                st.rerun()
            else:
                st.error("Can't book this yet: " + res.message)
        return
    st.info(f"Ready to record a confirmed booking worth {fmt_money(pending['value'], pending['currency'])}.")
    for w in pending["warnings"]:
        st.warning(w)
    a, b, _ = st.columns([1.2, 1, 3])
    if a.button("Yes, record booking", type="primary", key=f"ok_{iid}"):
        with db() as s:
            res = wf.decide_proposal(s, app_settings(), pending["proposal_id"], approve=True)
        state.pop(iid, None)
        if res.ok:
            st.success("Booking recorded.")
        else:
            st.error(res.message)
    if b.button("Cancel", key=f"no_{iid}"):
        with db() as s:
            wf.decide_proposal(s, app_settings(), pending["proposal_id"], approve=False, note="Cancelled by owner")
        state.pop(iid, None)
        st.rerun()


def _compare_tab() -> None:
    with db() as s:
        inquiries = list(s.scalars(select(Inquiry).where(Inquiry.status.notin_(["closed"])).order_by(Inquiry.updated_at.desc())))
        options = {i.id: inquiry_label(i) for i in inquiries}
    if not options:
        empty_state("No inquiries yet", "Add an inquiry first, then record the quotes you receive for it.")
        return
    ids = list(options)
    pre = st.session_state.get("selected_inquiry")
    iid: str = st.selectbox("Trip", ids, index=ids.index(pre) if pre in ids else 0, format_func=lambda k: options[k]) or ids[0]
    with db() as s:
        inq = s.get(Inquiry, iid)
        assert inq is not None
        kv_panel([("Route", f"{inq.origin or '?'} → {inq.destination or '?'}"), ("Departure", when(inq)),
                  ("Passengers", inq.passenger_count or "not stated"),
                  ("Budget", fmt_money(inq.budget_amount, inq.currency) if inq.budget_amount else "not given"),
                  ("Asked for", ", ".join(SERVICE_LABELS.get(x, x) for x in inq.requested_services) or "nothing extra")])
        quotes = list_quotes_for_inquiry(s, iid)
        default_ccy = inq.currency or (quotes[0].currency if quotes else "USD")
        comp = compare_quotes(inq, quotes) if quotes else None
    rfq_panel(iid)
    prefill = operator_pdf_reader(iid, f"opdf_{iid}")
    _add_form(iid, default_ccy, prefill)
    if not quotes:
        empty_state("No quotes yet", "Add the quotes you've received from operators to compare them here.")
        return
    st.markdown("#### Quotes")
    for q in quotes:
        with st.container(border=True):
            a, b = st.columns([3, 2.2])
            fees = f" + {q.additional_fees:,.0f} fees" if q.additional_fees else ""
            a.markdown(f"**{q.operator_label}** · {q.aircraft_type or 'aircraft not stated'} · {q.passenger_capacity or '?'} seats  \n"
                       f"{q.total_price:,.0f}{fees} {q.currency} " + status_badge(q.status, "quote")
                       + (f" · sent {q.presented_at:%d %b}" if q.presented_at else "")
                       + f"  \n<span style='color:#6B7280;font-size:0.8rem'>Entered manually · {fmt_dt(q.created_at)}"
                       + (" · fictional demo quote" if q.is_sample else "") + "</span>", unsafe_allow_html=True)
            allowed = [t for t in ACTIONS if t in QUOTE_TRANSITIONS[QuoteStatus(q.status)]]
            cols = b.columns(max(1, len(allowed)))
            for col, t in zip(cols, allowed, strict=False):
                if col.button(ACTIONS[t], key=f"{q.id}_{t.value}"):
                    try:
                        with db() as s:
                            change_quote_status(s, q.id, t)
                        st.rerun()
                    except Exception as exc:  # noqa: BLE001
                        show_exception(exc)

    assert comp is not None
    st.markdown("#### Side-by-side")
    st.caption(f"Prices compared in {comp.comparison_currency} including listed fees. All figures are calculated from the "
               "entered quotes; nothing is looked up or estimated.")
    for n in comp.notes:
        st.info(n)
    table = pd.DataFrame([{
        "Operator": x.operator_label, "All-in price": x.total_price, "Currency": x.currency,
        "vs cheapest": None if x.difference_from_lowest is None else x.difference_from_lowest,
        "Seats": {"sufficient": "✓ enough", "insufficient": "✗ too few", "unknown": "? not stated"}[x.capacity_status],
        "Budget": {"within": "✓ within", "over": "✗ over", "no_budget": "—", "currency_mismatch": "other currency"}[x.budget_status],
        "Cheapest": "★" if x.is_lowest else "",
        "Compared": "yes" if x.comparable else "no — " + (x.exclusion_reason or ""),
    } for x in comp.assessments])
    st.dataframe(table, hide_index=True, width="stretch",
                 column_config={"All-in price": st.column_config.NumberColumn(format="localized"),
                                "vs cheapest": st.column_config.NumberColumn(format="localized")})
    cols = st.columns(min(3, len(comp.assessments)))
    for i, x in enumerate(comp.assessments):
        with cols[i % len(cols)], st.container(border=True):
            st.markdown(f"**{x.operator_label}** " + (badge("cheapest", "good") if x.is_lowest else ""), unsafe_allow_html=True)
            for adv in x.advantages:
                st.markdown(f"✓ {adv}")
            for dis in x.disadvantages:
                st.markdown(f"✗ {dis}")
            if x.operator_questions:
                st.markdown("*Ask the operator:*")
                for qn in x.operator_questions[:5]:
                    st.markdown(f"- {qn}")
    quote_pdf_panel(iid, f"qpdf_{iid}")
    client, _ = live_client()
    if client is not None:
        with st.expander("✨ Explain this comparison in plain English (AI)"):
            st.caption("The AI only rephrases the figures above. If it mentions any number that isn't in the comparison, "
                       "its explanation is withheld.")
            if st.button("Explain", key=f"ex_{iid}"):
                with st.spinner("Writing…"), db() as s:
                    res = wf.explain_quotes(s, app_settings(), iid, client)
                if not res.ok:
                    st.error(res.message)
                elif res.data.get("explanation"):
                    st.markdown(res.data["explanation"])
                    st.caption("AI-written summary of the calculated comparison.")
                else:
                    st.warning("The AI explanation was withheld because it contained figures that aren't in your quotes.")
    st.divider()
    _booking(iid, quotes)


def _bookings_tab() -> None:
    with db() as s:
        rows = [(b.id, b.inquiry.customer_label, f"{b.inquiry.origin or '?'} → {b.inquiry.destination or '?'}",
                 b.booked_value, b.currency, b.status, b.confirmed_at, b.service_category, b.is_sample)
                for b in s.scalars(select(Booking).order_by(Booking.confirmed_at.desc()))]
    if not rows:
        empty_state("No bookings yet", "Record a booking from a quote once it's confirmed.")
        return
    show = st.segmented_control("Show", ["active", "all"], default="active", key="bk_show",
                                format_func=lambda v: {"active": "Upcoming", "all": "All"}[v])
    for bid, who, route, value, ccy, status, confirmed, cat, sample in rows:
        if show == "active" and status != BookingStatus.CONFIRMED.value:
            continue
        with st.container(border=True):
            a, b = st.columns([3, 2])
            a.markdown(f"**{who}** · {route} · {fmt_money(value, ccy)} " + status_badge(status)
                       + f"  \n<span style='color:#6B7280;font-size:0.8rem'>{cat.replace('_', ' ')} · confirmed "
                       f"{fmt_date(confirmed.date() if confirmed else None)}" + (" · sample" if sample else "") + "</span>",
                       unsafe_allow_html=True)
            allowed = BOOKING_TRANSITIONS[BookingStatus(status)] if has_permission(Permission.MANAGE_BOOKINGS) else set()
            if BookingStatus.COMPLETED in allowed and b.button("Trip completed", key=f"cp_{bid}"):
                with db() as s:
                    complete_booking(s, bid)
                st.rerun()
            if BookingStatus.CANCELLED in allowed:
                with b.popover("Cancel booking"):
                    reason = st.text_input("Reason", key=f"cr_{bid}")
                    if st.button("Confirm cancellation", key=f"cc_{bid}", disabled=not reason.strip()):
                        try:
                            with db() as s:
                                cancel_booking(s, bid, reason=reason)
                            st.rerun()
                        except Exception as exc:  # noqa: BLE001
                            show_exception(exc)


def render() -> None:
    page_header("Quotes", "Compare operator quotes side by side and record bookings once they're confirmed.")
    t1, t2, t3 = st.tabs(["⚖️ Compare quotes", "🗓️ Bookings", "🛩️ Operators"])
    with t1:
        _compare_tab()
    with t2:
        _bookings_tab()
    with t3:
        operators_tab()
