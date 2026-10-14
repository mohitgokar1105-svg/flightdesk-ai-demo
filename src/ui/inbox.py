"""Inbox: the team's work queue. Answers "what needs my attention, what's next, and how do I do it" on one page —
open any item to work on it right here."""

from __future__ import annotations

import html

import streamlit as st

from src.auth import ROLE_LABELS, Permission, current_principal
from src.domain import FollowUpStatus
from src.exceptions import FlightDeskError
from src.models import utcnow
from src.services.followup_service import change_followup_status
from src.services.workspace_service import QueueItem, work_queue
from src.ui.common import badge, db, empty_state, goto, page_header, show_exception
from src.ui.inquiries import render_detail
from src.ui.visuals import hero

SECTIONS = [("new", "New"), ("in_progress", "In progress"), ("awaiting_customer", "Waiting on customer"),
            ("followups_due", "Follow-ups due"), ("recently_completed", "Recently done")]
EMPTY = {
    "new": ("No new inquiries", "New requests from the website or that you add from an email appear here."),
    "in_progress": ("Nothing in progress", "Inquiries waiting for details or ready to quote appear here."),
    "awaiting_customer": ("No quotes waiting", "Once you send a quote, the inquiry waits here for the customer's answer."),
    "followups_due": ("No follow-ups due today", "You're up to date."),
    "recently_completed": ("Nothing finished this week yet", "Completed follow-ups, bookings and closed inquiries show here."),
}
TAG_TONE = {"From website": "accent", "Overdue": "bad", "Due today": "warn", "Waiting on customer": "info"}


def _open(iid: str) -> None:
    st.session_state["inbox_open"] = iid
    st.rerun()


def _row(item: QueueItem, n: int, section: str) -> None:
    with st.container(border=True):
        a, b = st.columns([4, 1.25])
        tag = badge(item.tag, TAG_TONE.get(item.tag, "neutral")) if item.tag else ""
        a.markdown(f"**{html.escape(item.title)}** {tag}  \n<span style='color:#5B6478;font-size:0.86rem'>"
                   f"{html.escape(item.detail)}</span>", unsafe_allow_html=True)
        if section == "followups_due" and item.followup_id:
            x, y = b.columns(2)
            if x.button("Open", key=f"ib_{section}_{n}", width="stretch"):
                _open(item.inquiry_id)
            if y.button("Done", key=f"ibd_{section}_{n}", width="stretch", help="Mark this follow-up as done"):
                try:
                    with db() as s:
                        change_followup_status(s, item.followup_id, FollowUpStatus.COMPLETED)
                    st.toast("Follow-up marked as done.")
                    st.rerun()
                except FlightDeskError as exc:
                    show_exception(exc)
        elif b.button("Open", key=f"ib_{section}_{n}", width="stretch", type="primary" if section == "new" else "secondary"):
            _open(item.inquiry_id)


def render() -> None:
    page_header("Inbox", "What needs your attention — open an item to work on it.", permission=Permission.VIEW_OPERATIONS)
    opened = st.session_state.get("inbox_open")
    if opened:
        if st.button("← Back to inbox", key="inbox_back"):
            st.session_state.pop("inbox_open", None)
            st.rerun()
        render_detail(opened)
        return

    with db() as s:
        queue = work_queue(s)
    counts = queue.counts()
    principal = current_principal()
    name = principal.display_name if principal else ""
    first = "" if not name or name in ROLE_LABELS.values() else name.split()[0]  # demo accounts are named by role
    hour = utcnow().hour
    greeting = "Good morning" if hour < 12 else ("Good afternoon" if hour < 18 else "Good evening")
    web = sum(1 for i in queue.new if i.tag == "From website")
    overdue = sum(1 for i in queue.followups_due if i.urgent)
    parts = []
    if counts["new"]:
        parts.append(f"{counts['new']} new request{'s' if counts['new'] != 1 else ''} to review"
                     + (f" ({web} from the website)" if web else ""))
    if counts["followups_due"]:
        parts.append(f"{counts['followups_due']} follow-up{'s' if counts['followups_due'] != 1 else ''} due"
                     + (f", {overdue} overdue" if overdue else ""))
    line = (" · ".join(parts) + ".") if parts else "You're all caught up — nothing new or due right now."
    hero(f"{greeting}, {first}" if first else greeting, line, eyebrow=f"{utcnow():%A %d %B}",
         chips=[f"{counts['new']} new", f"{counts['in_progress']} in progress",
                f"{counts['awaiting_customer']} waiting on customer", f"{counts['followups_due']} follow-ups due"])
    st.markdown(" ")
    a, b, _ = st.columns([1.6, 1.6, 3])
    if queue.new and a.button("Open the next new inquiry →", type="primary", width="stretch"):
        _open(queue.new[0].inquiry_id)
    if b.button("➕ Add an inquiry from an email", width="stretch"):
        goto("inquiries", inquiries_tab="add")
    names = dict(SECTIONS)
    default = next((k for k, _ in SECTIONS if counts[k]), SECTIONS[0][0])
    picked = st.segmented_control(
        "Section", list(names), default=default, key="inbox_section", label_visibility="collapsed",
        format_func=lambda k: names[k] if k == "recently_completed" else f"{names[k]} ({counts[k]})")
    key = picked if picked in names else default
    items: list[QueueItem] = getattr(queue, key)
    if not items:
        empty_state(*EMPTY[key])
        return
    for n, item in enumerate(items):
        _row(item, n, key)
