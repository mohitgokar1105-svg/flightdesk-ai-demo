"""AI Assistant support: suggested questions and data-only answers when the AI service is unavailable.

The data-only answers are computed by the same deterministic functions the dashboard uses and are labelled
"Answered from your records (AI not used)". They never imitate model output.
"""

from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.domain import QuoteStatus
from src.models import Quote, utcnow
from src.services.dashboard_service import (
    PROFIT_NOTE,
    business_kpis,
    needs_attention,
    plain_summary,
    recent_activity,
    resolve_period,
    summary_facts,
)

SUGGESTED_QUESTIONS = [
    "How is the business doing this month?",
    "Which inquiries need follow-up?",
    "What should I prioritize today?",
    "Summarize recent customer inquiries.",
    "Which quotes are awaiting review?",
    "Draft a follow-up email.",
    "Explain our conversion rate.",
    "Show the data supporting this conclusion.",
]

_ROUTES: list[tuple[str, re.Pattern[str]]] = [  # order matters: first match wins
    ("evidence", re.compile(r"\b(data|evidence)\b.*\b(support|behind|show)|\bshow\b.*\b(data|evidence)\b", re.I)),
    ("recent_inquiries", re.compile(r"\b(recent|latest|new)\b.*\binquir", re.I)),
    ("draft", re.compile(r"\b(draft|write)\b", re.I)),
    ("priorities", re.compile(r"\bpriorit\w*|\bfocus\b|\bwhat should i\b", re.I)),
    ("conversion", re.compile(r"\bconver(sion|t)\w*", re.I)),
    ("followups", re.compile(r"\bfollow[- ]?ups?\b|\boverdue\b|\bneed\w*\b.*\b(reply|chase)\b", re.I)),
    ("today", re.compile(r"\btoday\b|\bhappened\b|\brecent activity\b", re.I)),
    ("quotes", re.compile(r"\bquotes?\b", re.I)),
    ("performance", re.compile(r"\bhow is\b|\bhow's\b|\bdoing\b|\bperformance\b|\bbusiness\b|\bmonth\b|\bsummary\b|\boverview\b", re.I)),
]


def route_question(question: str) -> str | None:
    for name, pattern in _ROUTES:
        if pattern.search(question or ""):
            return name
    return None


def answer_without_ai(session: Session, question: str, now: datetime | None = None) -> tuple[str, bool]:
    """Return (markdown answer, matched). Unmatched questions get an honest explanation, not a guess."""
    now = now or utcnow()
    route = route_question(question)
    month = resolve_period("this_month", now.date())
    if route == "performance":
        lines = plain_summary(summary_facts(session, month, now))
        return "\n".join(f"- {line}" for line in lines) + f"\n\n_{PROFIT_NOTE}_", True
    if route in ("followups", "priorities"):
        items = needs_attention(session, now)
        if route == "followups":
            items = [i for i in items if i.kind in ("followup_overdue", "followup_today", "quote_no_reply", "missing_details")]
        if not items:
            return "Nothing needs follow-up right now. 🎉", True
        head = "Here's what to tackle first:" if route == "priorities" else "These need a follow-up:"
        body = "\n".join(f"{n}. **{i.title}** — {i.detail}" for n, i in enumerate(items[:8], start=1))
        more = f"\n\n…and {len(items) - 8} more on the Overview page." if len(items) > 8 else ""
        return f"{head}\n\n{body}{more}", True
    if route == "today":
        events = [e for e in recent_activity(session, 30) if e["time"].date() == now.date()]
        if not events:
            return "No business activity has been recorded today yet.", True
        return "Today so far:\n\n" + "\n".join(f"- {e['time']:%H:%M} — {e['text']}" + (f" ({e['who']})" if e["who"] else "")
                                             for e in events[:12]), True
    if route == "quotes":
        received = list(session.scalars(select(Quote).where(Quote.status == QuoteStatus.RECEIVED.value)))
        sent = list(session.scalars(select(Quote).where(Quote.status == QuoteStatus.PRESENTED.value)))
        if not received and not sent:
            return "No quotes are waiting on anyone right now.", True
        parts = []
        if received:
            parts.append("**To review and send** (received from operators):\n" + "\n".join(
                f"- {q.inquiry.customer_label}: {q.operator_label}, {q.all_in_price:,.0f} {q.currency}" for q in received[:10]))
        if sent:
            parts.append("**Sent — waiting for the customer's decision:**\n" + "\n".join(
                f"- {q.inquiry.customer_label}: {q.operator_label}, {q.all_in_price:,.0f} {q.currency}"
                + (f" (sent {q.presented_at:%d %b})" if q.presented_at else "") for q in sent[:10]))
        return "\n\n".join(parts), True
    if route == "conversion":
        k = business_kpis(session, resolve_period("last_30_days", now.date()), now)  # counts only; no money
        rate = "not available yet (no inquiries in the period)" if k.conversion_rate is None else f"{k.conversion_rate:.0%}"
        return (f"Over the last 30 days, **{k.conversion_numerator} of {k.conversion_denominator}** inquiries received have a "
                f"confirmed or completed booking — a booking conversion rate of **{rate}**.\n\n"
                "How it's counted: each inquiry counts once, however many quotes or bookings it has; cancelled bookings "
                "don't count. Recent inquiries may still convert, so the rate for the latest weeks tends to rise later."), True
    if route == "recent_inquiries":
        from src.models import Inquiry

        rows = list(session.scalars(select(Inquiry).order_by(Inquiry.created_at.desc()).limit(8)))
        if not rows:
            return "No inquiries have been recorded yet.", True
        lines = [f"- **{i.customer_label}** — {i.origin or '?'} → {i.destination or '?'}, "
                 f"{i.passenger_count or '?'} passengers, received {i.created_at:%d %b}"
                 + (f" · still need: {', '.join(m['field'].replace('_', ' ') for m in i.missing_information)}"
                    if i.missing_information else "") for i in rows]
        return "Most recent inquiries:\n\n" + "\n".join(lines), True
    if route == "evidence":
        return ("Each answer has a **Data used** section underneath it showing the records it was based on. "
                "Open it to check the figures yourself."), True
    if route == "draft":
        return ("Open **Follow-ups**, pick the customer and the type of email, and a draft is written from their records "
                "(with placeholders for anything we don't know). Drafts are never sent automatically."), True
    return ("I can answer questions about inquiries, quotes, follow-ups and performance once the AI assistant is set up. "
            "Until then, try one of the suggested questions — they're answered straight from your records."), False
