"""Customer portal: public flight requests and status checks.

Trust boundary
--------------
Website visitors run as :data:`src.auth.PORTAL`, which holds exactly one permission (``SUBMIT_INQUIRY``). The two
public operations below check that permission and then perform one narrow, fixed write or read with system
privileges. They never return internal data: no notes, prices, quotes, operators, other customers or analytics.

* :func:`submit_flight_request` validates the form, creates the inquiry in the same database the team uses
  (``source="customer_portal"``) and returns a reference. Submitting the same request twice returns the
  first reference instead of creating a duplicate.
* :func:`check_request_status` needs the reference **and** the email used on the request. A wrong combination gives
  the same "not found" answer as a missing reference, so references can't be used to discover other customers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src import clock
from src.auth import SYSTEM, Permission, as_principal, require
from src.exceptions import DuplicateRecord
from src.models import Customer, Inquiry, Quote
from src.schemas import InquiryCreate, validate_email, validate_phone
from src.security import detect_prompt_injection
from src.services.audit_service import record_audit
from src.services.inquiry_service import compute_content_hash, create_inquiry

SOURCE_PORTAL = "customer_portal"
MAX_PASSENGERS = 50
MAX_DAYS_AHEAD = 730
REFERENCE_RE = re.compile(r"^INQ-[0-9A-F]{10}$")
TIME_PREFERENCES = ["Not sure yet", "Early morning (before 9am)", "Morning", "Afternoon", "Evening"]
AIRCRAFT_PREFERENCES = ["No preference", "Turboprop", "Light jet", "Midsize jet", "Super-midsize jet", "Heavy jet",
                        "Not sure — please advise"]
TRIP_TYPE_LABELS = {"one_way": "One way", "round_trip": "Round trip"}

# What a customer is told about their request. Internal statuses are never shown verbatim.
CUSTOMER_STATUS = {
    "new": ("Received", "Our team will review your request and contact you by email."),
    "needs_information": ("We need a few details", "We'll email you to confirm a few details before we look for aircraft."),
    "qualified": ("Finding options", "We're checking aircraft options for your trip."),
    "quoted_preparing": ("Finding options", "We're checking aircraft options for your trip."),
    "quoted": ("Options sent", "We've sent you options by email. Reply to that email if you have questions."),
    "booked": ("Booked", "Your trip is booked in our records. Your advisor will be in touch with the details."),
    "completed": ("Trip completed", "Thank you for flying with us."),
    "closed": ("Closed", "This request is closed. You're welcome to send a new request at any time."),
}


class FlightRequest(BaseModel):
    """The customer-facing request form. Every rule here has a friendly message in :data:`FIELD_LABELS`."""

    full_name: str = Field(min_length=2, max_length=80)
    email: str = Field(max_length=200)
    phone: str | None = Field(default=None, max_length=40)
    origin: str = Field(min_length=2, max_length=120)
    destination: str = Field(min_length=2, max_length=120)
    trip_type: Literal["one_way", "round_trip"] = "one_way"
    departure_date: date
    return_date: date | None = None
    passengers: int = Field(ge=1, le=MAX_PASSENGERS)
    time_preference: str | None = Field(default=None, max_length=40)
    aircraft_preference: str | None = Field(default=None, max_length=60)
    special_requests: str | None = Field(default=None, max_length=900)

    @field_validator("full_name", "origin", "destination", "time_preference", "aircraft_preference", "special_requests",
                     "phone", mode="before")
    @classmethod
    def _strip(cls, v: object) -> object:
        if isinstance(v, str):
            v = " ".join(v.split()) if "\n" not in v else v.strip()
            return v or None
        return v

    @field_validator("full_name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not any(c.isalpha() for c in v):
            raise ValueError("Please enter your name.")
        return v

    @field_validator("email", mode="before")
    @classmethod
    def _email(cls, v: object) -> object:
        email = validate_email(v)
        if email is None:
            raise ValueError("Please enter your email address so we can reply.")
        return email

    @field_validator("phone", mode="before")
    @classmethod
    def _phone(cls, v: object) -> object:
        return validate_phone(v)

    @model_validator(mode="after")
    def _trip_rules(self) -> FlightRequest:
        today = clock.now().date()
        if self.origin.strip().lower() == self.destination.strip().lower():
            raise ValueError("destination|Departure and destination must be different places.")
        if self.departure_date < today:
            raise ValueError("departure_date|The departure date can't be in the past.")
        if self.departure_date > today + timedelta(days=MAX_DAYS_AHEAD):
            raise ValueError("departure_date|Please choose a departure date within the next two years.")
        if self.trip_type == "round_trip":
            if self.return_date is None:
                raise ValueError("return_date|Please choose a return date, or switch to one way.")
            if self.return_date < self.departure_date:
                raise ValueError("return_date|The return date must be on or after the departure date.")
        else:
            self.return_date = None
        if self.time_preference == TIME_PREFERENCES[0]:
            self.time_preference = None
        if self.aircraft_preference == AIRCRAFT_PREFERENCES[0]:
            self.aircraft_preference = None
        return self


FIELD_LABELS = {"full_name": "Full name", "email": "Email", "phone": "Phone", "origin": "From", "destination": "To",
                "trip_type": "Trip", "departure_date": "Departure date", "return_date": "Return date",
                "passengers": "Passengers", "time_preference": "Preferred time", "aircraft_preference": "Aircraft",
                "special_requests": "Special requests"}

REQUIRED_MESSAGES = {"origin": "Where are you flying from?", "destination": "Where are you flying to?",
                     "departure_date": "Please choose a departure date.", "passengers": "How many people are travelling?",
                     "full_name": "Please enter your name.", "email": "Please enter your email address so we can reply."}


def friendly_errors(exc: ValidationError) -> dict[str, str]:
    """Map pydantic errors to {field: plain-English message} for the form."""
    out: dict[str, str] = {}
    for e in exc.errors():
        msg = str(e.get("msg", "")).replace("Value error, ", "")
        field = str(e["loc"][0]) if e.get("loc") else ""
        if "|" in msg:  # model-level rule tagged with its field
            field, msg = msg.split("|", 1)
        etype = e.get("type", "")
        if etype == "missing" or (etype in ("string_too_short", "string_type") and e.get("input") in ("", None)):
            msg = REQUIRED_MESSAGES.get(field, f"Please fill in {FIELD_LABELS.get(field, field).lower()}.")
        elif etype == "string_too_short":
            msg = f"{FIELD_LABELS.get(field, field)} looks too short."
        elif etype == "string_too_long":
            msg = f"{FIELD_LABELS.get(field, field)} is too long."
        elif field == "passengers" and etype in ("greater_than_equal", "less_than_equal", "int_type", "int_parsing"):
            msg = f"Passengers must be between 1 and {MAX_PASSENGERS}. For larger groups, mention it in special requests."
        elif etype in ("date_type", "date_parsing", "date_from_datetime_parsing"):
            msg = "Please choose a date."
        out.setdefault(field or "form", msg)
    return out


@dataclass(frozen=True)
class SubmissionResult:
    reference: str
    duplicate: bool  # True when the same request had already been received (nothing new was created)


def compose_request_text(req: FlightRequest) -> str:
    """The request as a readable message for the team (and for the AI reader). Customer text stays labelled."""
    lines = [
        "Flight request submitted through the customer website form.",
        f"Name: {req.full_name}",
        f"Email: {req.email}",
        f"Phone: {req.phone or 'not given'}",
        f"From: {req.origin}",
        f"To: {req.destination}",
        f"Trip: {TRIP_TYPE_LABELS[req.trip_type]}",
        f"Departure date: {req.departure_date.isoformat()}" + (f" ({req.time_preference})" if req.time_preference else ""),
    ]
    if req.return_date:
        lines.append(f"Return date: {req.return_date.isoformat()}")
    lines.append(f"Passengers: {req.passengers}")
    if req.aircraft_preference:
        lines.append(f"Aircraft preference: {req.aircraft_preference}")
    if req.special_requests:
        lines.append(f"Special requests (customer's words): {req.special_requests}")
    return "\n".join(lines)


def _special_requirements(req: FlightRequest) -> str | None:
    parts = []
    if req.aircraft_preference:
        parts.append(f"Aircraft preference: {req.aircraft_preference}")
    if req.special_requests:
        parts.append(req.special_requests)
    return "; ".join(parts)[:1000] or None


def submit_flight_request(session: Session, req: FlightRequest) -> SubmissionResult:
    """Save a customer's request as a new inquiry for the team. Idempotent for identical resubmissions."""
    require(Permission.SUBMIT_INQUIRY)
    text = compose_request_text(req)
    content_hash = compute_content_hash(req.full_name, text)
    with as_principal(SYSTEM):  # narrow, fixed write on behalf of the website visitor
        existing = session.scalar(select(Inquiry.id).where(Inquiry.content_hash == content_hash))
        if existing:
            return SubmissionResult(existing, duplicate=True)
        customer = session.scalar(select(Customer).where(func.lower(Customer.email) == req.email.lower()))
        name_differs = customer is not None and customer.name.strip().lower() != req.full_name.strip().lower()
        if customer is None:  # match by email only — a name alone never links a request to an existing customer
            customer = Customer(name=req.full_name, email=req.email, phone=req.phone)
            session.add(customer)
            session.flush()
            record_audit(session, "customer_created", actor="system", entity_type="customer", entity_id=customer.id,
                         metadata={"channel": SOURCE_PORTAL, "has_phone": bool(req.phone)})
        signals = detect_prompt_injection(req.special_requests or "")
        data = InquiryCreate(
            customer_label=req.full_name, customer_email=req.email, customer_phone=req.phone, original_text=text,
            origin=req.origin, destination=req.destination, trip_type=req.trip_type, departure_date=req.departure_date,
            return_date=req.return_date, departure_time_preference=req.time_preference,
            special_requirements=_special_requirements(req), passenger_count=req.passengers,
            date_flexibility="fixed")
        metadata: dict[str, Any] = {
            "method": SOURCE_PORTAL,
            "portal": {"name_on_form": req.full_name, "phone_on_form": req.phone,
                       "aircraft_preference": req.aircraft_preference, "time_preference": req.time_preference,
                       "name_differs_from_customer_record": name_differs, "suspicious_instructions": signals},
        }
        try:
            inquiry = create_inquiry(session, data, source=SOURCE_PORTAL, actor="system", customer=customer,
                                     extraction_metadata=metadata)
        except DuplicateRecord:  # the same request arrived a moment earlier
            existing = session.scalar(select(Inquiry.id).where(Inquiry.content_hash == content_hash))
            if existing:
                return SubmissionResult(existing, duplicate=True)
            raise
        record_audit(session, "customer_request_submitted", actor="system", entity_type="inquiry", entity_id=inquiry.id,
                     metadata={"channel": SOURCE_PORTAL, "suspicious_instructions": bool(signals)})
        return SubmissionResult(inquiry.id, duplicate=False)


def check_request_status(session: Session, reference: str, email: str) -> dict[str, Any] | None:
    """Customer-safe status for one request, or None if the reference + email combination doesn't match."""
    require(Permission.SUBMIT_INQUIRY)
    ref = (reference or "").strip().upper()
    mail = (email or "").strip().lower()
    if not REFERENCE_RE.match(ref) or not mail:
        return None
    with as_principal(SYSTEM):
        inq = session.get(Inquiry, ref)
        if inq is None or inq.customer is None or not inq.customer.email or inq.customer.email.lower() != mail:
            return None
        key = inq.status
        if key == "quoted":
            presented = session.scalar(select(Quote.id).where(Quote.inquiry_id == inq.id,
                                                              Quote.status.in_(["presented", "accepted"])).limit(1))
            key = "quoted" if presented else "quoted_preparing"
        label, detail = CUSTOMER_STATUS.get(key, ("Received", CUSTOMER_STATUS["new"][1]))
        return {"reference": inq.id, "status": label, "detail": detail,
                "route": f"{inq.origin or '?'} → {inq.destination or '?'}",
                "departure_date": inq.departure_date, "return_date": inq.return_date,
                "passengers": inq.passenger_count, "submitted": inq.created_at}
