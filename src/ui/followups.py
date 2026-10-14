"""Follow-ups: write customer emails from the inquiry's records (AI or template) and keep a to-do list.
FlightDesk never sends email — drafts are copied or opened in the owner's own email app."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any

import streamlit as st
from sqlalchemy import select

from src.ai.drafting import mailto_link
from src.domain import (
    FOLLOWUP_CLOSED_STATUSES,
    FOLLOWUP_EDITABLE_STATUSES,
    FOLLOWUP_TRANSITIONS,
    FOLLOWUP_TYPE_LABELS,
    PRIMARY_EMAIL_TYPES,
    FollowUpStatus,
    FollowUpType,
    Tone,
)
from src.models import FollowUp, Inquiry
from src.schemas import FollowUpCreate, FollowUpDraft
from src.services.followup_service import change_followup_status, is_overdue, update_followup
from src.services.rfq_service import to_chat_message, whatsapp_link
from src.ui.common import (
    app_settings,
    badge,
    db,
    empty_state,
    inquiry_label,
    live_client,
    page_header,
    show_exception,
    status_badge,
)
from src.workflows import workflow_engine as wf

OTHER_TYPES = [t for t in FollowUpType if t not in PRIMARY_EMAIL_TYPES]


def _due_picker(prefix: str, default: datetime | None) -> datetime | None:
    a, b = st.columns(2)
    base = default or (datetime.now(UTC) + timedelta(days=1))
    d = a.date_input("Follow up by", value=base.date(), key=f"{prefix}_d", format="DD/MM/YYYY")
    t = b.time_input("Time (UTC)", value=base.time().replace(second=0, microsecond=0), key=f"{prefix}_t")
    return datetime.combine(d, t or time(9, 0), tzinfo=UTC) if d else None


def _write_tab() -> None:
    with db() as s:
        inquiries = list(s.scalars(select(Inquiry).order_by(Inquiry.updated_at.desc())))
        options = {i.id: inquiry_label(i) for i in inquiries}
        emails = {i.id: (i.customer.email if i.customer else None) for i in inquiries}
        phones = {i.id: (i.customer.phone if i.customer else None) for i in inquiries}
    if not options:
        empty_state("No customers to write to yet", "Add an inquiry first — emails are written from its details.")
        return
    preselect = st.session_state.get("selected_inquiry")
    ids = list(options)
    iid: str = st.selectbox("Customer / trip", ids, index=ids.index(preselect) if preselect in ids else 0,
                            format_func=lambda k: options[k]) or ids[0]
    a, b, c = st.columns([1.6, 1, 1])
    type_choices = [t.value for t in PRIMARY_EMAIL_TYPES] + [t.value for t in OTHER_TYPES]
    preset = st.session_state.get("email_type_preset")
    ftype = FollowUpType(a.selectbox("Type of email", type_choices, index=type_choices.index(preset) if preset in type_choices else 0,
                                     format_func=lambda v: FOLLOWUP_TYPE_LABELS[FollowUpType(v)]))
    tone = Tone(b.selectbox("Tone", [t.value for t in Tone], format_func=str.capitalize))
    client, _ = live_client()
    use_ai = c.toggle("Write with AI", value=client is not None, disabled=client is None,
                      help=None if client else "AI isn't set up — a template is used instead.")
    state = st.session_state.setdefault("email_ui", {})
    if state.get("inquiry_id") != iid:
        state.clear()
        state["inquiry_id"] = iid
    label = "Write it again" if state.get("draft") else "Write email"
    if st.button(label, type="primary"):
        with st.spinner("Writing from the customer's records…"), db() as s:
            res = wf.start_followup_draft(s, app_settings(), iid, ftype, tone, client=client if use_ai else None,
                                          supersede_run_id=state.get("run_id"))
        if res.ok:
            draft = res.data["draft"]
            state.update(run_id=res.run_id, draft=draft.model_dump(), ftype=ftype.value, tone=tone.value)
            st.session_state["email_subject"] = draft.subject
            st.session_state["email_body"] = draft.body
            st.rerun()
        else:
            st.error(f"Couldn't write the email: {res.message}. Try again, or switch off “Write with AI” to use a template.")
    draft = FollowUpDraft.model_validate(state["draft"]) if state.get("draft") else None
    if not draft:
        st.caption("Emails only use what's in the records — prices, dates and confirmations are never invented. "
                   "Anything unknown becomes a [[placeholder]] for you to fill in.")
        return
    st.markdown(badge("Written by AI" if draft.method == "openai" else "Written from a template",
                      "accent" if draft.method == "openai" else "neutral"), unsafe_allow_html=True)
    for w in draft.warnings:
        st.warning(f"Check before sending: {w}")
    if draft.placeholders:
        st.info("Fill in before sending: " + ", ".join(dict.fromkeys(p.strip("[]") for p in draft.placeholders)))
    subject = st.text_input("Subject", key="email_subject")
    body = st.text_area("Email", key="email_body", height=300)
    due = _due_picker("email_due", datetime.now(UTC) + timedelta(days=2))
    a, b, c = st.columns([1, 1.2, 1.2])
    if a.button("Save draft", type="primary"):
        try:
            data = FollowUpCreate(inquiry_id=iid, followup_type=FollowUpType(state["ftype"]), tone=Tone(state["tone"]),
                                  subject=subject, body=body, due_at=due, generation_method=draft.method)
            with db() as s:
                res = wf.save_followup_from_run(s, app_settings(), state["run_id"], data)
            if res.ok:
                state.clear()
                st.success("Draft saved to your to-do list. It has not been sent.")
            else:
                st.error(res.message)
        except Exception as exc:  # noqa: BLE001
            show_exception(exc)
    b.link_button("Open in my email app", mailto_link(emails.get(iid), subject, body),
                  help="Opens your own email program with this draft. FlightDesk doesn't send anything.")
    c.link_button("💬 Send via WhatsApp", whatsapp_link(to_chat_message(subject, body), phones.get(iid)),
                  help="Opens WhatsApp with this message ready (to the customer's number if we have it). Nothing is "
                       "sent until you press send in WhatsApp.")
    with st.expander("Copy text"):
        st.code(f"Subject: {subject}\n\n{body}", language=None)


def _todo_tab() -> None:
    a, b = st.columns([1.5, 2])
    show = a.segmented_control("Show", ["open", "done"], default="open", key="fu_show",
                               format_func=lambda v: {"open": "To do", "done": "Done & cancelled"}[v])
    query = b.text_input("Search", placeholder="Customer or subject", label_visibility="collapsed")
    with db() as s:
        rows = list(s.scalars(select(FollowUp).order_by(FollowUp.due_at.is_(None), FollowUp.due_at)))
        closed = {x.value for x in FOLLOWUP_CLOSED_STATUSES}
        rows = [f for f in rows if (f.status in closed) == (show == "done")]
        if query.strip():
            q = query.strip().lower()
            rows = [f for f in rows if q in (f.subject + " " + f.inquiry.customer_label).lower()]
        items = [(f.id, f.inquiry.customer_label, f.subject, f.status, f.due_at, is_overdue(f),
                  FOLLOWUP_TYPE_LABELS.get(FollowUpType(f.followup_type), f.followup_type)) for f in rows]
    if not items:
        empty_state("Nothing here", "Write an email to add it to your to-do list." if show == "open" else "No finished follow-ups yet.")
        return
    selected = st.session_state.get("selected_followup")
    for fid, who, subject, status, due, overdue, kind in items[:60]:
        with st.container(border=True):
            c1, c2 = st.columns([5, 1])
            due_txt = ("Overdue · was due " if overdue else "Due ") + due.strftime("%d %b, %H:%M") if due else "No due date"
            c1.markdown(f"**{who}** — {subject}  \n{kind} · " + status_badge(status, "followup")
                        + (" " + badge(due_txt, "bad") if overdue else f" · {due_txt}"), unsafe_allow_html=True)
            if c2.button("Open" if selected != fid else "Close", key=f"op_{fid}"):
                st.session_state["selected_followup"] = None if selected == fid else fid
                st.rerun()
            if selected == fid:
                _followup_detail(fid)


def _followup_detail(fid: str) -> None:
    with db() as s:
        f = s.get(FollowUp, fid)
        assert f is not None
        status = FollowUpStatus(f.status)
        allowed = FOLLOWUP_TRANSITIONS[status]
        data: dict[str, Any] = {"subject": f.subject, "body": f.body, "due": f.due_at, "notes": f.advisor_notes or "",
                "email": f.inquiry.customer.email if f.inquiry.customer else None,
                "phone": f.inquiry.customer.phone if f.inquiry.customer else None}
    editable = status in FOLLOWUP_EDITABLE_STATUSES
    with st.form(f"fu_edit_{fid}"):
        subject = st.text_input("Subject", value=data["subject"], disabled=not editable)
        body = st.text_area("Email", value=data["body"], height=220, disabled=not editable)
        due = _due_picker(f"fu_due_{fid}", data["due"])
        notes = st.text_input("Your notes", value=data["notes"])
        if st.form_submit_button("Save changes"):
            try:
                with db() as s:
                    update_followup(s, fid, subject=subject if editable else None, body=body if editable else None,
                                    due_at=due, advisor_notes=notes)
                st.success("Saved.")
            except Exception as exc:  # noqa: BLE001
                show_exception(exc)
    a, b, c, d, e = st.columns(5)
    if FollowUpStatus.COMPLETED in allowed and a.button("✓ Mark as sent / done", key=f"done_{fid}", type="primary"):
        with db() as s:
            change_followup_status(s, fid, "completed", notes="Marked done by owner (sent outside FlightDesk).")
        st.session_state["selected_followup"] = None
        st.rerun()
    if b.button("Snooze 1 day", key=f"sn_{fid}"):
        with db() as s:
            update_followup(s, fid, due_at=(data["due"] or datetime.now(UTC)) + timedelta(days=1))
        st.rerun()
    if FollowUpStatus.CANCELLED in allowed and c.button("Cancel follow-up", key=f"cx_{fid}"):
        with db() as s:
            change_followup_status(s, fid, "cancelled")
        st.session_state["selected_followup"] = None
        st.rerun()
    d.link_button("Open in email app", mailto_link(data["email"], data["subject"], data["body"]))
    e.link_button("💬 WhatsApp", whatsapp_link(to_chat_message(data["subject"], data["body"]), data.get("phone")))
    if not editable:
        st.caption("This email was approved, so its text is locked. Notes and due date can still change.")


def render() -> None:
    page_header("Follow-ups", "Write customer emails in seconds and never miss a follow-up. Nothing is sent automatically.")
    write, todo = st.tabs(["✉️ Write an email", "✅ To-do list"])
    with write:
        _write_tab()
    with todo:
        _todo_tab()
