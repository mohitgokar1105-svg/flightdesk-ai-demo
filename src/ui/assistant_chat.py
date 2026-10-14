"""AI Assistant: a chat connected to the business's records through allowlisted tools.

Read questions are answered from tool results; anything that would change records is shown as a proposal with
Approve / Discard buttons. When the AI service is unavailable, suggested questions are answered straight from the
records and labelled as such.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from src.ai.assistant import SUGGESTED_QUESTIONS, answer_without_ai
from src.auth import Permission
from src.domain import INQUIRY_STATUS_LABELS
from src.exceptions import FlightDeskError
from src.services.document_service import create_quote_pdf, pdf_filename
from src.ui.common import ai_status, app_settings, db, goto, live_client, page_header
from src.workflows import workflow_engine as wf

HISTORY_TURNS = 8
TOOL_NAMES = {
    "get_dashboard_summary": "business summary", "get_inquiries": "inquiries", "get_inquiry_details": "inquiry details",
    "get_follow_ups_due": "follow-ups", "get_quotes": "quotes", "get_booking_summary": "bookings",
    "get_business_performance": "performance", "search_customers": "customers", "compare_quotes": "quote comparison",
    "prepare_inquiry": "new inquiry", "draft_email": "email draft", "mark_follow_up_complete": "follow-ups",
    "propose_inquiry_status_change": "inquiry status", "record_recommendation_decision": "suggestions",
    "prepare_quote_pdf": "customer PDF",
}
ERRORS = {
    "ai_transient": "The AI service is busy or unreachable right now. Please try again in a moment.",
    "ai_request_rejected": "The AI service turned the request down — the API key or model may need checking in Settings.",
    "ai_invalid_output": "The AI gave an answer I couldn't use. Please try rephrasing.",
    "ai_not_configured": "The AI assistant isn't set up.",
}


def _chat() -> dict[str, Any]:
    return st.session_state.setdefault("chat", {"messages": [], "history": []})


def _prior_items(chat: dict[str, Any]) -> list[Any]:
    return [item for turn in chat["history"][-HISTORY_TURNS:] for item in turn]


def _ask(question: str) -> None:
    chat = _chat()
    chat["messages"].append({"role": "user", "content": question})
    client, _ = live_client()
    if client is None:
        with db() as s:
            answer, _ = answer_without_ai(s, question)
        chat["messages"].append({"role": "assistant", "content": answer, "source": "data"})
        return
    with db() as s:
        res = wf.run_chat_turn(s, app_settings(), _prior_items(chat), question, client)
    if res.ok:
        chat["history"].append(res.data["items"])
        used = list(dict.fromkeys(TOOL_NAMES.get(t["tool"], t["tool"]) for t in res.data["tool_log"] if t["status"] != "rejected"))
        evidence = [{"tool": TOOL_NAMES.get(t["tool"], t["tool"]), "status": t["status"],
                     "data": t.get("data") or {}, "error": t.get("error")} for t in res.data["tool_log"]]
        pdfs = []
        for t in res.data["tool_log"]:
            if t["tool"] == "prepare_quote_pdf" and t["status"] == "executed" and (t.get("data") or {}).get("pdf_ready"):
                try:
                    with db() as s:
                        data, facts = create_quote_pdf(s, t["data"]["inquiry_id"],
                                                       show_operator_names=bool(t["data"].get("operator_names_shown")))
                    pdfs.append({"bytes": data, "name": pdf_filename(facts), "customer": facts["customer"],
                                 "options": len(facts["options"])})
                except FlightDeskError as exc:
                    pdfs.append({"error": str(exc)})
        chat["messages"].append({"role": "assistant", "content": res.data["answer"], "source": "ai", "used": used,
                                 "evidence": evidence, "pdfs": pdfs,
                                 "proposals": [dict(p, outcome=None) for p in res.data["proposals"]],
                                 "intake_runs": res.data["intake_run_ids"], "run_id": res.run_id})
        return
    message = ERRORS.get(res.data.get("error_category", ""), res.message)
    with db() as s:
        fallback, matched = answer_without_ai(s, question)
    content = message + (f"\n\nHere's what I can tell you straight from your records:\n\n{fallback}" if matched else "")
    chat["messages"].append({"role": "assistant", "content": content, "source": "error", "run_id": res.run_id})


def _decide(proposal: dict[str, Any], approve: bool) -> None:
    chat = _chat()
    with db() as s:
        res = wf.decide_proposal(s, app_settings(), proposal["proposal_id"], approve=approve,
                                 note=None if approve else "Discarded in chat")
    if approve and res.data.get("proposal_status") == "executed":
        result = res.data.get("result") or {}
        proposal["outcome"] = "done" + (f" · {result.get('followup_id') or result.get('inquiry_id') or ''}")
        note = f"The owner approved {proposal['proposal_id']} ({proposal['tool']}). Result: {result}."
    elif approve:
        proposal["outcome"] = "failed: " + res.message
        note = f"The owner approved {proposal['proposal_id']} but it failed: {res.message}"
    else:
        proposal["outcome"] = "discarded"
        note = f"The owner discarded {proposal['proposal_id']} ({proposal['tool']}). Nothing was changed."
    chat["history"].append([{"role": "developer", "content": note}])


def _proposal_card(i: int, p: dict[str, Any]) -> None:
    args = p["arguments"]
    with st.container(border=True):
        if p["tool"] == "draft_email":
            st.markdown(f"**Save this email draft?** — {args.get('draft_subject', '')}")
            st.text(args.get("draft_body", ""))
            for w in args.get("draft_warnings") or []:
                st.warning(f"Check before sending: {w}")
            if args.get("draft_note"):
                st.caption(args["draft_note"])
        elif p["tool"] == "mark_follow_up_complete":
            st.markdown(f"**Mark this follow-up as done?** {p.get('rationale') or ''}")
        elif p["tool"] == "propose_inquiry_status_change":
            st.markdown(f"**Move inquiry {args.get('inquiry_id')} to “"
                        f"{INQUIRY_STATUS_LABELS.get(args.get('target_status'), args.get('target_status'))}”?** "
                        f"{args.get('reason', '')}")
        else:
            st.markdown(f"**{p['tool'].replace('_', ' ').capitalize()}?**")
        if p.get("outcome"):
            text = p["outcome"]
            (st.success if text.startswith("done") else st.info if text == "discarded" else st.error)(
                "Saved." if text.startswith("done") else ("Discarded — nothing was changed." if text == "discarded" else text))
            return
        a, b, _ = st.columns([1, 1, 4])
        if a.button("Approve", key=f"ap_{i}_{p['proposal_id']}", type="primary"):
            _decide(p, True)
            st.rerun()
        if b.button("Discard", key=f"ds_{i}_{p['proposal_id']}"):
            _decide(p, False)
            st.rerun()


def _render_messages() -> None:
    for i, msg in enumerate(_chat()["messages"]):
        with st.chat_message(msg["role"], avatar="🧑‍💼" if msg["role"] == "user" else "✨"):
            st.markdown(msg["content"])
            if msg["role"] != "assistant":
                continue
            if msg.get("source") == "data":
                st.caption("Answered from your records (AI not used).")
            elif msg.get("source") == "ai" and msg.get("used"):
                st.caption("Checked: " + ", ".join(msg["used"]))
            if msg.get("evidence"):
                with st.expander("Data used for this answer"):
                    st.caption("Exactly what the assistant read from your records (limited to what your role can see).")
                    for ev in msg["evidence"]:
                        st.markdown(f"**{ev['tool']}** — {'blocked: ' + str(ev['error']) if ev['status'] == 'rejected' else ev['status']}")
                        if ev["data"]:
                            st.json(ev["data"], expanded=False)
            for n, pdf in enumerate(msg.get("pdfs") or []):
                if pdf.get("error"):
                    st.warning(f"Couldn't make the PDF: {pdf['error']}")
                    continue
                st.download_button(f"⬇ Download PDF — {pdf['customer']} ({pdf['options']} option"
                                   f"{'s' if pdf['options'] != 1 else ''})", pdf["bytes"], file_name=pdf["name"],
                                   mime="application/pdf", key=f"pdf_{i}_{n}", type="primary")
                st.caption("Made from your recorded quotes. Check it before sharing — nothing has been sent.")
            for p in msg.get("proposals") or []:
                _proposal_card(i, p)
            for run_id in msg.get("intake_runs") or []:
                if st.button("Review & save this inquiry", key=f"rv_{i}_{run_id}", type="primary"):
                    goto("inquiries", open_intake_run=run_id)


def render() -> None:
    page_header("AI Assistant", "Ask about your business in plain English. Answers come from your records.",
                show_demo_note=False, permission=Permission.USE_ASSISTANT)
    ready, status = ai_status()
    chat = _chat()
    top_l, top_r = st.columns([4, 1])
    top_l.caption(status if ready else status + " Until then, the suggested questions below are answered from your records.")
    if top_r.button("New conversation", width="stretch", disabled=not chat["messages"]):
        st.session_state["chat"] = {"messages": [], "history": []}
        st.rerun()
    if not ready:
        with st.expander("How to switch the AI assistant on"):
            st.markdown("1. Create an API key at platform.openai.com.\n"
                        "2. In the project folder, open the `.env` file and set `OPENAI_API_KEY=` to your key and "
                        "`DEMO_MODE=false`.\n3. Restart FlightDesk. The key is never shown or stored in the database.")

    pending = st.session_state.pop("chat_pending", None)
    if pending:
        with st.spinner("Looking at your records…"):
            _ask(pending)

    if not chat["messages"]:
        st.markdown("#### Try asking")
        cols = st.columns(2)
        for n, q in enumerate(SUGGESTED_QUESTIONS):
            if cols[n % 2].button(q, key=f"sq_{n}", width="stretch"):
                with st.spinner("Looking at your records…"):
                    _ask(q)
                st.rerun()
        st.caption("You can also paste a customer's message and say “add this inquiry” — you'll review it before it's saved.")
    _render_messages()
    question = st.chat_input("Ask about inquiries, quotes, follow-ups, bookings…")
    if question:
        with st.spinner("Looking at your records…"):
            _ask(question)
        st.rerun()
