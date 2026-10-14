"""Customers: contact details, their trips, and what they've booked."""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st
from pydantic import ValidationError

from src.auth import Permission, has_permission
from src.schemas import CustomerCreate
from src.services.customer_service import (
    customer_summaries,
    find_or_create_customer,
    get_customer,
    update_customer,
)
from src.ui.common import badge, db, empty_state, goto, page_header, show_exception, status_badge, when


def _booked(value: dict[str, float]) -> str:
    return ", ".join(f"{v:,.0f} {c}" for c, v in value.items()) or "—"


def _detail(cid: str) -> None:
    with db() as s:
        c = get_customer(s, cid)
        info = {"name": c.name, "email": c.email or "", "phone": c.phone or "", "company": c.company or "",
                "notes": c.notes or "", "sample": c.is_sample}
        trips = [(i.id, f"{i.origin or '?'} → {i.destination or '?'}", when(i), i.status, i.created_at) for i in
                 sorted(c.inquiries, key=lambda x: x.created_at, reverse=True)]
    st.markdown(f"### {info['name']}" + (" " + badge("sample", "neutral") if info["sample"] else ""), unsafe_allow_html=True)
    left, right = st.columns([1, 1.2])
    with left, st.form(f"cust_{cid}"):
        name = st.text_input("Name", value=info["name"])
        email = st.text_input("Email", value=info["email"])
        phone = st.text_input("Phone", value=info["phone"])
        company = st.text_input("Company", value=info["company"])
        notes = st.text_area("Notes", value=info["notes"], height=80)
        if st.form_submit_button("Save", type="primary"):
            try:
                with db() as s:
                    update_customer(s, cid, CustomerCreate(name=name, email=email or None, phone=phone or None,
                                                           company=company or None, notes=notes or None))
                st.success("Saved.")
            except ValidationError as exc:
                for e in exc.errors():
                    st.error(f"{str(e['loc'][-1]).capitalize()}: {e['msg'].replace('Value error, ', '')}")
            except Exception as exc:  # noqa: BLE001
                show_exception(exc)
    with right:
        st.markdown("**Trips**")
        if not trips:
            st.caption("No inquiries yet.")
        for iid, route, dep, status, created in trips:
            a, b = st.columns([4, 1])
            a.markdown(f"{route} · {dep} " + status_badge(status) + f"  \n<span style='color:#6B7280;font-size:0.8rem'>"
                       f"received {created:%d %b %Y}</span>", unsafe_allow_html=True)
            if b.button("Open", key=f"ct_{iid}"):
                goto("inquiries", inquiries_tab="list", selected_inquiry=iid)


def render() -> None:
    page_header("Customers", "Everyone who has asked for a trip, with their contact details and bookings.")
    a, b = st.columns([3, 1])
    query = a.text_input("Search customers", placeholder="Name, email or company", label_visibility="collapsed")
    with b.popover("➕ Add customer", width="stretch"), st.form("new_customer", clear_on_submit=True):
        name = st.text_input("Name")
        email = st.text_input("Email")
        phone = st.text_input("Phone")
        if st.form_submit_button("Add", type="primary"):
            try:
                with db() as s:
                    c = find_or_create_customer(s, CustomerCreate(name=name, email=email or None, phone=phone or None))
                    st.session_state["selected_customer"] = c.id
                st.rerun()
            except ValidationError as exc:
                for e in exc.errors():
                    st.error(f"{str(e['loc'][-1]).capitalize()}: {e['msg'].replace('Value error, ', '')}")
            except Exception as exc:  # noqa: BLE001
                show_exception(exc)
    with db() as s:
        rows = customer_summaries(s, query)
    if not rows:
        empty_state("No customers yet", "Customers are added automatically when you save an inquiry.")
        return
    table = pd.DataFrame([{"id": r["customer_id"], "Name": r["name"], "Email": r["email"] or "—", "Phone": r["phone"] or "—",
                           "Trips": r["inquiries"], "Open": r["open_inquiries"], "Booked": _booked(r["booked_value"]),
                           "Last activity": r["last_activity"].strftime("%d %b %Y")} for r in rows])
    shown = table.drop(columns=["id"] if has_permission(Permission.VIEW_FINANCIALS) else ["id", "Booked"])
    event: Any = st.dataframe(shown, hide_index=True, width="stretch", on_select="rerun",
                              selection_mode="single-row", key="cust_table")
    st.caption(("Booked = confirmed and completed bookings, per currency. " if has_permission(Permission.VIEW_FINANCIALS)
                else "") + "Select a row to open a customer.")
    picked = event.selection.rows if event and hasattr(event, "selection") else []
    if picked:
        st.session_state["selected_customer"] = table.iloc[picked[0]]["id"]
    cid = st.session_state.get("selected_customer")
    if cid:
        st.divider()
        try:
            _detail(cid)
        except Exception as exc:  # noqa: BLE001
            st.session_state["selected_customer"] = None
            show_exception(exc)
