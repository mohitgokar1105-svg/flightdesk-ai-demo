"""Documents: the customer-facing "charter options" PDF, and reading text out of an operator's quote PDF.

Everything printed in the customer PDF comes from stored records (inquiry + recorded quotes) or airport coordinates.
The PDF never says a flight is booked or a price is guaranteed. Operator names are hidden by default (options are
labelled A, B, C), because brokers usually don't share them before booking.
"""

from __future__ import annotations

import io
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fpdf import FPDF
from fpdf.enums import XPos, YPos
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from sqlalchemy.orm import Session

from src.auth import Permission, require
from src.config import APP_NAME
from src.exceptions import NotFound, ValidationFailure
from src.models import Inquiry, utcnow
from src.places import distance_km, resolve_place
from src.schemas import QUOTE_LINE_ITEMS, LLMQuoteExtraction
from src.services.audit_service import record_audit

FONT_DIR = Path(__file__).resolve().parents[1] / "reference" / "fonts"
LIVE_QUOTE_STATUSES = ("received", "presented", "accepted")
MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_PDF_PAGES = 15
MAX_TEXT_CHARS = 20_000
ITEM_LABELS = {
    "catering": "catering", "ground_transport": "ground transport", "hotel": "hotel", "concierge": "concierge",
    "pet_transport": "pet transport", "extra_baggage": "extra baggage", "wifi": "Wi-Fi",
    "landing_and_handling_fees": "landing & handling fees", "taxes": "taxes", "de_icing": "de-icing",
    "crew_overnight": "crew overnight", "positioning": "positioning",
}
DISCLAIMER = ("These are options received from aircraft operators, listed for your information. They are not a booking "
              "and prices are not guaranteed: availability and final prices must be confirmed before anything is booked. "
              "Nothing is booked or charged until you confirm with your advisor.")
DEMO_NOTE = f"{APP_NAME} is an independent demo project, not affiliated with Flygreen or any aircraft operator."


# ---------------------------------------------------------------------------- facts
def _money(amount: float, currency: str) -> str:
    return f"{amount:,.0f} {currency}" if float(amount).is_integer() else f"{amount:,.2f} {currency}"


def _date(d: date | datetime | None) -> str | None:
    return d.strftime("%a %d %b %Y") if d else None


def proposal_facts(session: Session, inquiry_id: str, *, show_operator_names: bool = False) -> dict[str, Any]:
    """Everything the customer PDF shows, taken from the records. Raises NotFound / ValidationFailure."""
    require(Permission.VIEW_OPERATIONS)
    inq = session.get(Inquiry, inquiry_id)
    if inq is None:
        raise NotFound(f"Inquiry {inquiry_id} not found.")
    today = utcnow().date()
    candidates = [q for q in inq.quotes if q.status in LIVE_QUOTE_STATUSES]
    expired = [q for q in candidates if q.valid_until is not None and q.valid_until < today]
    live = sorted((q for q in candidates if q not in expired), key=lambda q: (q.currency, q.all_in_price))
    if not live:
        raise ValidationFailure("There are no live quotes for this trip yet. Record at least one quote first."
                                if not expired else "Every quote for this trip has passed its validity date. Ask the "
                                "operators for updated quotes first.")
    a, b = resolve_place(inq.origin), resolve_place(inq.destination)
    km = distance_km(a, b) if a and b and a != b else None
    options = []
    for n, q in enumerate(live):
        letter = chr(ord("A") + n) if n < 26 else str(n + 1)
        options.append({
            "letter": letter, "title": q.operator_label if show_operator_names else f"Option {letter}",
            "aircraft": q.aircraft_type or "Aircraft to be confirmed", "seats": q.passenger_capacity,
            "price": q.total_price, "fees": q.additional_fees or 0.0, "fees_description": q.fees_description,
            "all_in": q.all_in_price, "currency": q.currency,
            "included": [ITEM_LABELS.get(x, x.replace("_", " ")) for x in (q.included_services or [])],
            "excluded": [ITEM_LABELS.get(x, x.replace("_", " ")) for x in (q.excluded_services or [])],
            "conditions": q.restrictions, "valid_until": _date(q.valid_until),
            "enough_seats": None if (q.passenger_capacity is None or inq.passenger_count is None)
            else q.passenger_capacity >= inq.passenger_count,
        })
    return {
        "reference": inq.id, "customer": inq.customer_label, "prepared_on": _date(utcnow()),
        "origin": inq.origin or "To be confirmed", "destination": inq.destination or "To be confirmed",
        "trip_type": {"one_way": "One way", "round_trip": "Round trip", "multi_leg": "Multi-stop"}.get(inq.trip_type or ""),
        "departure": _date(inq.departure_date), "return": _date(inq.return_date),
        "preferred_time": inq.departure_time_preference, "passengers": inq.passenger_count,
        "requests": inq.special_requirements,
        "distance_km": round(km) if km else None, "distance_nm": round(km / 1.852) if km else None,
        "options": options, "show_operator_names": show_operator_names,
        "left_out_expired": [f"{q.operator_label} (valid until {q.valid_until:%d %b %Y})" for q in expired
                             if q.valid_until is not None],
    }


def default_cover_note(facts: dict[str, Any]) -> str:
    first = facts["customer"].split()[0] if facts.get("customer") else ""
    n = len(facts["options"])
    return (f"Dear {first}," if first else "Hello,") + (
        f" thank you for your request. Below {'is the option' if n == 1 else f'are the {n} options'} we've received for "
        f"your trip from {facts['origin']} to {facts['destination']}. All options are subject to availability and final "
        "confirmation — just reply to let us know which you prefer, or if anything should change.")


# ---------------------------------------------------------------------------- PDF
NAVY, INK, MUTED, LINE, ACCENT, SOFT = (11, 27, 51), (14, 26, 43), (110, 120, 145), (232, 227, 217), (47, 107, 219), (246, 243, 236)


class _ProposalPDF(FPDF):
    def __init__(self, reference: str) -> None:
        super().__init__(format="A4")
        self.reference = reference
        self.add_font("DejaVu", "", str(FONT_DIR / "DejaVuSans.ttf"))
        self.add_font("DejaVu", "B", str(FONT_DIR / "DejaVuSans-Bold.ttf"))
        self.set_auto_page_break(auto=True, margin=22)
        self.set_margins(18, 18, 18)

    def footer(self) -> None:
        self.set_y(-15)
        self.set_font("DejaVu", "", 7.5)
        self.set_text_color(*MUTED)
        self.cell(0, 5, f"{self.reference} · Page {self.page_no()} of {{nb}} · {DEMO_NOTE}", align="C")


def build_quote_pdf(facts: dict[str, Any], cover_note: str | None = None) -> bytes:
    """Render the customer "charter options" PDF from :func:`proposal_facts`."""
    pdf = _ProposalPDF(facts["reference"])
    pdf.alias_nb_pages()
    pdf.add_page()
    w = pdf.w - pdf.l_margin - pdf.r_margin
    # header band
    pdf.set_fill_color(*NAVY)
    pdf.rect(0, 0, pdf.w, 46, style="F")
    pdf.set_draw_color(143, 180, 255)
    pdf.set_line_width(0.5)
    for i in range(0, 30, 5):  # dashed flight path, kept clear of the header text
        pdf.line(pdf.w - 50 + i, 34 - i * 0.62, pdf.w - 47.5 + i, 32.45 - i * 0.62)
    pdf.set_fill_color(143, 211, 182)
    pdf.ellipse(pdf.w - 52.5, 33.5, 3.2, 3.2, style="F")
    pdf.set_fill_color(255, 255, 255)
    pdf.ellipse(pdf.w - 20, 12.5, 4, 4, style="F")
    pdf.set_xy(18, 12)
    pdf.set_text_color(143, 211, 182)
    pdf.set_font("DejaVu", "B", 8)
    pdf.cell(0, 5, f"{APP_NAME.upper()} · PRIVATE CHARTER", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("DejaVu", "B", 19)
    pdf.cell(0, 11, "Your charter options", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("DejaVu", "", 9)
    pdf.set_text_color(203, 214, 234)
    pdf.cell(0, 5, f"Prepared for {facts['customer']} · {facts['prepared_on']} · Reference {facts['reference']}")
    pdf.set_y(54)
    pdf.set_text_color(*INK)
    if cover_note:
        pdf.set_font("DejaVu", "", 10)
        pdf.multi_cell(w, 5.4, cover_note, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(4)
    # trip summary panel
    rows = [("Route", f"{facts['origin']}  →  {facts['destination']}")]
    if facts.get("distance_km"):
        rows.append(("Distance", f"≈ {facts['distance_km']:,} km ({facts['distance_nm']:,} nm) straight-line"))
    for label, key in (("Trip", "trip_type"), ("Departure", "departure"), ("Return", "return"),
                       ("Preferred time", "preferred_time"), ("Passengers", "passengers"), ("Requests", "requests")):
        if facts.get(key):
            rows.append((label, str(facts[key])))
    top = pdf.get_y()
    pdf.set_fill_color(*SOFT)
    pdf.set_draw_color(*LINE)
    height = 8 + 6.2 * len(rows) + (6 if facts.get("requests") and len(str(facts["requests"])) > 70 else 0)
    pdf.rect(pdf.l_margin, top, w, height, style="DF")
    pdf.set_xy(pdf.l_margin + 5, top + 4)
    for label, value in rows:
        pdf.set_x(pdf.l_margin + 5)
        pdf.set_font("DejaVu", "", 8.5)
        pdf.set_text_color(*MUTED)
        pdf.cell(32, 6.2, label)
        pdf.set_font("DejaVu", "B" if label == "Route" else "", 9.5)
        pdf.set_text_color(*INK)
        pdf.multi_cell(w - 42, 6.2, value, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_y(top + height + 7)
    # options
    pdf.set_font("DejaVu", "B", 12)
    pdf.cell(0, 7, f"{len(facts['options'])} option{'s' if len(facts['options']) != 1 else ''}",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("DejaVu", "", 8.5)
    pdf.set_text_color(*MUTED)
    currencies = sorted({o["currency"] for o in facts["options"]})
    pdf.multi_cell(w, 5, "All-in price = quoted price plus any extra fees the operator listed. Sorted from lowest price"
                   + (f" within each currency ({', '.join(currencies)}); currencies are not converted." if len(currencies) > 1
                      else "."), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)
    for opt in facts["options"]:
        lines = [f"{opt['aircraft']}" + (f" · {opt['seats']} seats" if opt["seats"] else "")]
        if opt["fees"]:
            lines.append(f"Quoted {_money(opt['price'], opt['currency'])} + fees {_money(opt['fees'], opt['currency'])}"
                         + (f" ({opt['fees_description']})" if opt["fees_description"] else ""))
        if opt["included"]:
            lines.append("Included: " + ", ".join(opt["included"]))
        if opt["excluded"]:
            lines.append("Not included: " + ", ".join(opt["excluded"]))
        if opt["conditions"]:
            lines.append("Conditions: " + opt["conditions"])
        if opt["valid_until"]:
            lines.append(f"Quote valid until {opt['valid_until']}")
        if opt["enough_seats"] is False:
            lines.append("Note: fewer seats than your party size.")
        needed = 14 + 5.2 * sum(1 + len(x) // 95 for x in lines)
        if pdf.get_y() + needed > pdf.h - 24:
            pdf.add_page()
        y = pdf.get_y()
        pdf.set_draw_color(*LINE)
        pdf.set_fill_color(255, 255, 255)
        pdf.rect(pdf.l_margin, y, w, needed, style="DF")
        pdf.set_fill_color(*ACCENT)
        pdf.rect(pdf.l_margin, y, 1.6, needed, style="F")
        pdf.set_xy(pdf.l_margin + 6, y + 4)
        pdf.set_font("DejaVu", "B", 11)
        pdf.set_text_color(*INK)
        pdf.cell(w - 70, 6, opt["title"])
        pdf.set_font("DejaVu", "B", 12)
        pdf.set_text_color(*NAVY)
        pdf.cell(58, 6, _money(opt["all_in"], opt["currency"]), align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("DejaVu", "", 9)
        pdf.set_text_color(68, 80, 106)
        for line in lines:
            pdf.set_x(pdf.l_margin + 6)
            pdf.multi_cell(w - 12, 5.2, line, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_y(y + needed + 4)
    pdf.ln(2)
    if pdf.get_y() > pdf.h - 48:  # keep the "Important" note in one piece
        pdf.add_page()
    pdf.set_font("DejaVu", "B", 9)
    pdf.set_text_color(*INK)
    pdf.cell(0, 5, "Important", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("DejaVu", "", 8.5)
    pdf.set_text_color(*MUTED)
    pdf.multi_cell(w, 4.6, DISCLAIMER + " Distances are straight-line between airports; flight times vary.",
                   new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    return bytes(pdf.output())


def create_quote_pdf(session: Session, inquiry_id: str, *, show_operator_names: bool = False,
                     cover_note: str | None = None, actor: str = "advisor") -> tuple[bytes, dict[str, Any]]:
    """Build the PDF and record that it was created (nothing is sent)."""
    facts = proposal_facts(session, inquiry_id, show_operator_names=show_operator_names)
    data = build_quote_pdf(facts, cover_note if cover_note is not None else default_cover_note(facts))
    record_audit(session, "quote_pdf_created", actor=actor, entity_type="inquiry", entity_id=inquiry_id,
                 metadata={"options": len(facts["options"]), "operator_names_shown": show_operator_names})
    return data, facts


def pdf_filename(facts: dict[str, Any]) -> str:
    name = re.sub(r"[^A-Za-z0-9]+", "-", facts["customer"]).strip("-")[:30] or "customer"
    return f"charter-options-{name}-{facts['reference']}.pdf"


# ---------------------------------------------------------------------------- reading PDFs
def read_pdf_text(data: bytes) -> str:
    """Text from an uploaded PDF (operator quote). Raises ValidationFailure with a plain-English reason."""
    if not data:
        raise ValidationFailure("The file is empty.")
    if len(data) > MAX_PDF_BYTES:
        raise ValidationFailure("That PDF is larger than 10 MB. Please upload the quote page only.")
    if not data.lstrip()[:5].startswith(b"%PDF"):
        raise ValidationFailure("That file isn't a PDF.")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ValidationFailure("That PDF is password-protected. Please upload an unlocked copy.")
        pages = reader.pages[:MAX_PDF_PAGES]
        text = "\n".join((p.extract_text() or "") for p in pages)
    except ValidationFailure:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError) as exc:
        raise ValidationFailure("That PDF couldn't be read. It may be damaged.") from exc
    text = re.sub(r"[ \t]+", " ", text).strip()
    if len(text) < 20:
        raise ValidationFailure("No text found in that PDF — it may be a scanned image. Please enter the quote manually.")
    return text[:MAX_TEXT_CHARS]


def _number_in_text(value: float, text: str) -> bool:
    """True if the amount appears in the document (ignoring thousands separators and decimals)."""
    digits = {re.sub(r"[^\d]", "", m) for m in re.findall(r"\d[\d,.' ]*\d|\d", text)}
    whole = str(int(round(value)))
    return any(d == whole or d.startswith(whole) and len(d) - len(whole) <= 2 for d in digits)


def quote_prefill(ex: LLMQuoteExtraction, text: str) -> tuple[dict[str, Any], list[str]]:
    """Turn the AI's reading into form values, dropping anything that isn't backed by the document."""
    from src.domain import SUPPORTED_CURRENCIES

    warnings: list[str] = []
    out: dict[str, Any] = {"operator": ex.operator_name, "aircraft": ex.aircraft_type, "fees_description": ex.fees_description,
                           "restrictions": ex.restrictions}
    if ex.passenger_capacity and 1 <= ex.passenger_capacity <= 500:
        out["capacity"] = ex.passenger_capacity
    for key, value in (("price", ex.total_price), ("fees", ex.additional_fees)):
        if value is None or value <= 0:
            continue
        if _number_in_text(value, text):
            out[key] = float(value)
        else:
            warnings.append(f"The AI's {'price' if key == 'price' else 'fees'} ({value:,.2f}) doesn't appear in the document, "
                            "so it was left blank — please enter it yourself.")
    ccy = (ex.currency or "").upper()
    if ccy in SUPPORTED_CURRENCIES:
        out["currency"] = ccy
    elif ccy:
        warnings.append(f"Currency '{ccy}' isn't supported here; please choose one.")
    out["included"] = [x for x in ex.included_services if x in QUOTE_LINE_ITEMS]
    out["excluded"] = [x for x in ex.excluded_services if x in QUOTE_LINE_ITEMS and x not in out["included"]]
    if ex.valid_until:
        try:
            out["valid_until"] = date.fromisoformat(ex.valid_until)
        except ValueError:
            warnings.append(f"Couldn't understand the validity date '{ex.valid_until}'.")
    if ex.embedded_instructions_detected:
        warnings.append("The document contains instructions aimed at software. They were ignored.")
    return {k: v for k, v in out.items() if v not in (None, "", [])}, warnings
