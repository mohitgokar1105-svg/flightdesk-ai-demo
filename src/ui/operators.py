"""Operator requests for quote (RFQ) panel and the operator directory. Messages open in WhatsApp / email, ready to send;
FlightDesk records what the team sent and marks replies automatically when a quote is recorded."""

from __future__ import annotations

import html
from typing import Any

import streamlit as st

from src.auth import Permission, has_permission
from src.exceptions import FlightDeskError
from src.models import Inquiry
from src.services.rfq_service import (
    CHANNELS,
    OperatorInput,
    chase_message,
    email_link,
    ensure_sample_operators,
    list_operators,
    record_requests_sent,
    requests_for,
    rfq_email_subject,
    rfq_message,
    save_operator,
    set_request_status,
    whatsapp_link,
)
from src.ui.common import badge, db, empty_state, show_exception

STATUS_TONE = {"sent": "warn", "replied": "good", "declined": "neutral", "no_response": "bad"}
CHANNEL_LABELS = {"whatsapp": "WhatsApp", "email": "Email", "phone": "Phone", "other": "Other"}


def _ago(hours: float) -> str:
    if hours < 1:
        return "just now"
    if hours < 48:
        return f"{int(hours)}h ago"
    return f"{int(hours // 24)} days ago"


def rfq_panel(iid: str) -> None:
    """Request quotes from operators for one inquiry, and see who has replied."""
    with db() as s:
        inq = s.get(Inquiry, iid)
        if inq is None:
            return
        message, subject = rfq_message(inq), rfq_email_subject(inq)
        trip = (inq.origin, inq.destination, inq.departure_date, inq.passenger_count)
        ops = [(o.id, o.name, o.base_airport, o.phone, o.email, o.fleet) for o in list_operators(s)]
        reqs = requests_for(s, iid)
        chase_texts = {r["id"]: chase_message(r["operator"], inq) for r in reqs if r["needs_chase"]}
    replied = sum(1 for r in reqs if r["status"] == "replied")
    with st.container(border=True):
        head = "**📨 Operator quote requests**"
        if reqs:
            head += f" &nbsp; {badge(f'{replied} of {len(reqs)} replied', 'good' if replied == len(reqs) else 'info')}"
        st.markdown(head, unsafe_allow_html=True)
        for r in reqs:
            a, b, c = st.columns([3.2, 1.3, 1.3])
            a.markdown(f"{html.escape(r['operator'])} {badge(r['label'], STATUS_TONE[r['status']])} "
                       f"<span style='color:#5B6478;font-size:0.82rem'>· {CHANNEL_LABELS.get(r['channel'], r['channel'])}, "
                       f"{_ago(r['hours_waiting'])}</span>", unsafe_allow_html=True)
            if r["status"] == "sent":
                if r["needs_chase"]:
                    b.link_button("💬 Chase", whatsapp_link(chase_texts[r["id"]], r["phone"]), width="stretch",
                                  help="Opens WhatsApp with a short follow-up to this operator.")
                if c.button("Declined", key=f"rqd_{r['id']}", width="stretch"):
                    _set(r["id"], "declined")
            elif r["status"] == "replied" and not r["quote_id"]:
                b.caption("Record their quote in Quotes")
        if not reqs:
            st.caption("Ask several operators at once. Their replies are tracked here, and recording an operator's quote "
                       "marks them as replied automatically.")
        if trip[0] is None or trip[1] is None:
            st.warning("Add the route first — operators can't quote without it.")
        with st.expander("Send a request to operators", expanded=not reqs):
            if not ops:
                empty_state("No operators in your directory yet", "Add them on Quotes → Operators.")
                if has_permission(Permission.ADMIN_DATA) and st.button("Add the fictional sample operators", key=f"seedops_{iid}"):
                    with db() as s:
                        ensure_sample_operators(s)
                    st.rerun()
                return
            text = st.text_area("Message (edit if you like)", value=message, height=240, key=f"rfqmsg_{iid}")
            picked = st.multiselect("Operators", [o[0] for o in ops], key=f"rfqops_{iid}",
                                    format_func=lambda oid: next(f"{o[1]} · {o[2] or 'base n/a'}" for o in ops if o[0] == oid),
                                    placeholder="Choose who to ask")
            for oid in picked:
                o = next(x for x in ops if x[0] == oid)
                x, y, z = st.columns([2.6, 1.2, 1.2])
                x.markdown(f"**{html.escape(o[1])}**  \n<span style='color:#5B6478;font-size:0.82rem'>{html.escape(o[5] or '')}"
                           "</span>", unsafe_allow_html=True)
                y.link_button("💬 WhatsApp", whatsapp_link(text, o[3]), width="stretch", disabled=not text.strip())
                z.link_button("✉️ Email", email_link(subject, text, o[4]), width="stretch", disabled=not text.strip())
            with st.popover("📋 Copy message"):
                st.code(text, language=None)
            a, b = st.columns([1.4, 2])
            channel: str = a.selectbox("Sent by", list(CHANNELS), format_func=lambda c: CHANNEL_LABELS[c],
                                       key=f"rfqch_{iid}") or "whatsapp"
            if b.button(f"✓ I've sent it to {len(picked)} operator{'s' if len(picked) != 1 else ''}", key=f"rfqsend_{iid}",
                        type="primary", disabled=not picked, width="stretch"):
                try:
                    with db() as s:
                        record_requests_sent(s, iid, picked, channel, text)
                    st.session_state.pop(f"rfqops_{iid}", None)
                    st.toast("Requests recorded — replies will be tracked here.")
                    st.rerun()
                except FlightDeskError as exc:
                    show_exception(exc)
            st.caption("FlightDesk doesn't send anything itself: the buttons open WhatsApp or your email with the message "
                       "ready, then you confirm here that it went out.")


def _set(request_id: str, status: str) -> None:
    try:
        with db() as s:
            set_request_status(s, request_id, status)
        st.rerun()
    except FlightDeskError as exc:
        show_exception(exc)


def operators_tab() -> None:
    """The operator directory: who you ask for quotes, and how to reach them."""
    with db() as s:
        ops = [(o.id, o.name, o.contact_name, o.phone, o.email, o.base_airport, o.fleet, o.notes, len(o.requests))
               for o in list_operators(s, active_only=True)]
    if not ops:
        empty_state("No operators yet", "Add the operators you request quotes from — then one click asks them all.")
        if has_permission(Permission.ADMIN_DATA) and st.button("Add the fictional sample operators", key="seedops_tab"):
            with db() as s:
                ensure_sample_operators(s)
            st.rerun()
    for oid, name, contact, phone, email, base, fleet, notes, n_req in ops:
        with st.container(border=True):
            a, b, c = st.columns([3, 1.1, 1.1])
            a.markdown(f"**{html.escape(name)}** · {html.escape(base or 'base not set')}  \n"
                       f"<span style='color:#5B6478;font-size:0.84rem'>{html.escape(fleet or '')}"
                       f"{' · ' if fleet and contact else ''}{html.escape(contact or '')} · {n_req} request(s)</span>",
                       unsafe_allow_html=True)
            if phone:
                b.link_button("💬 WhatsApp", whatsapp_link("Hi! ", phone), width="stretch")
            if email:
                c.link_button("✉️ Email", email_link("", "", email), width="stretch")
            with st.expander("Edit"):
                _operator_form(f"op_{oid}", oid, {"name": name, "contact_name": contact, "phone": phone, "email": email,
                                                 "base_airport": base, "fleet": fleet, "notes": notes})
    with st.expander("➕ Add an operator", expanded=not ops):
        _operator_form("op_new", None, {})


def _operator_form(key: str, operator_id: str | None, v: dict[str, Any]) -> None:
    with st.form(key, clear_on_submit=operator_id is None):
        a, b = st.columns(2)
        name = a.text_input("Operator name", value=v.get("name") or "")
        contact = b.text_input("Contact person", value=v.get("contact_name") or "")
        a, b = st.columns(2)
        phone = a.text_input("WhatsApp / phone (with country code)", value=v.get("phone") or "", placeholder="+1 704 555 0100")
        email = b.text_input("Email", value=v.get("email") or "")
        a, b = st.columns(2)
        base = a.text_input("Home base", value=v.get("base_airport") or "", placeholder="e.g. Charlotte (CLT)")
        fleet = b.text_input("Fleet", value=v.get("fleet") or "", placeholder="e.g. Light & midsize jets")
        notes = st.text_input("Notes", value=v.get("notes") or "")
        if st.form_submit_button("Save operator", type="primary"):
            try:
                with db() as s:
                    save_operator(s, OperatorInput(name, contact or None, email or None, phone or None, base or None,
                                                   fleet or None, notes or None), operator_id)
                st.toast("Operator saved.")
                st.rerun()
            except FlightDeskError as exc:
                show_exception(exc)
