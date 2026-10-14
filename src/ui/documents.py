"""PDF widgets shared by the Quotes page and the inquiry workspace: the customer "charter options" PDF and reading an
operator's quote PDF into the quote form."""

from __future__ import annotations

from typing import Any

import streamlit as st

from src.exceptions import FlightDeskError
from src.services.document_service import create_quote_pdf, default_cover_note, pdf_filename, proposal_facts
from src.ui.common import app_settings, db, live_client, show_exception
from src.workflows import workflow_engine as wf


def quote_pdf_panel(iid: str, key: str) -> None:
    """Options + "Create PDF" + download. The PDF is built from recorded quotes only; nothing is sent."""
    client, _ = live_client()
    with st.container(border=True):
        st.markdown("**📄 Customer PDF** — your recorded quotes as a clean document to send yourself.")
        a, b = st.columns(2)
        names = a.toggle("Show operator names", value=False, key=f"{key}_names",
                         help="Off: options are labelled A, B, C (usual before booking).")
        modes = ["Standard note", "AI-written note (checked)"] if client else ["Standard note"]
        mode = b.selectbox("Cover note", modes + ["No note"], key=f"{key}_mode")
        if st.button("Create PDF", key=f"{key}_go", type="primary"):
            try:
                note: str | None
                warning = None
                with db() as s:
                    facts = proposal_facts(s, iid, show_operator_names=names)
                    note = default_cover_note(facts)
                if mode == "No note":
                    note = ""
                elif mode.startswith("AI") and client is not None:
                    with st.spinner("Writing the cover note…"), db() as s:
                        res = wf.write_cover_note(s, app_settings(), iid, client)
                    if res.ok and res.data.get("note"):
                        note = res.data["note"]
                    else:
                        warning = ("The AI note mentioned something not in your records, so the standard note was used."
                                   if res.ok else f"The AI note isn't available right now ({res.message}); the standard "
                                   "note was used.")
                with db() as s:
                    data, facts = create_quote_pdf(s, iid, show_operator_names=names, cover_note=note)
                if facts["left_out_expired"]:
                    expired_msg = ("Left out because their validity date has passed: " + "; ".join(facts["left_out_expired"])
                                   + ".")
                    warning = f"{warning} {expired_msg}" if warning else expired_msg
                st.session_state[f"{key}_pdf"] = {"bytes": data, "name": pdf_filename(facts), "note": note,
                                                  "warning": warning, "options": len(facts["options"])}
            except FlightDeskError as exc:
                show_exception(exc)
        made: dict[str, Any] | None = st.session_state.get(f"{key}_pdf")
        if made:
            if made["warning"]:
                st.info(made["warning"])
            st.download_button(f"⬇ Download PDF ({made['options']} option{'s' if made['options'] != 1 else ''})",
                               made["bytes"], file_name=made["name"], mime="application/pdf", key=f"{key}_dl")
            if made["note"]:
                with st.expander("Cover note used"):
                    st.write(made["note"])
            st.caption("Lists recorded quotes only and says nothing is booked. FlightDesk doesn't send it — attach it to "
                       "your own email, then mark the quote as sent.")


def operator_pdf_reader(iid: str, key: str) -> dict[str, Any] | None:
    """Upload an operator's quote PDF; returns form pre-fill values once read (the form is then checked and saved)."""
    client, _ = live_client()
    with st.expander("📎 Read an operator's quote PDF", expanded=bool(st.session_state.get(f"{key}_result"))):
        st.caption("Upload the PDF the operator sent. " + ("The AI reads it and fills in the form below for you to check."
                   if client else "AI is off, so the text is shown for you to copy into the form."))
        up = st.file_uploader("Operator quote (PDF)", type=["pdf"], key=f"{key}_file", label_visibility="collapsed")
        if up is not None and st.button("Read PDF", key=f"{key}_read", type="primary"):
            with st.spinner("Reading the quote…"), db() as s:
                res = wf.read_operator_quote_pdf(s, app_settings(), iid, up.getvalue(), client=client)
            st.session_state[f"{key}_result"] = {"ok": res.ok, "message": res.message, **res.data}
        result = st.session_state.get(f"{key}_result")
        if not result:
            return None
        if not result["ok"]:
            st.error(f"Couldn't read it: {result['message']} Nothing was saved.")
        else:
            if result.get("prefill"):
                st.success(f"Filled in {len(result['prefill'])} field(s) below — check every value against the PDF, "
                           "then Save quote.")
            elif result.get("method") == "none":
                st.info("Text extracted — copy the details into the form below.")
            else:
                st.warning("The AI couldn't find quote details it could verify in this document.")
            for w in result.get("warnings") or []:
                st.warning(w)
        if result.get("text"):
            with st.expander("Text found in the PDF"):
                st.text(result["text"][:6000])
        if st.button("Clear", key=f"{key}_clear"):
            st.session_state.pop(f"{key}_result", None)
            st.rerun()
        return result.get("prefill") if result.get("ok") else None
