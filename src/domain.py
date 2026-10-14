"""Domain vocabulary: statuses, permitted transitions, service categories and currencies.

All status changes in the application are validated against the transition tables defined here.
"""

from __future__ import annotations

from enum import StrEnum

from src.exceptions import InvalidTransition


class InquiryStatus(StrEnum):
    NEW = "new"
    NEEDS_INFORMATION = "needs_information"
    QUALIFIED = "qualified"
    QUOTED = "quoted"
    BOOKED = "booked"
    COMPLETED = "completed"
    CLOSED = "closed"


INQUIRY_TRANSITIONS: dict[InquiryStatus, set[InquiryStatus]] = {
    InquiryStatus.NEW: {InquiryStatus.NEEDS_INFORMATION, InquiryStatus.QUALIFIED, InquiryStatus.CLOSED},
    InquiryStatus.NEEDS_INFORMATION: {InquiryStatus.QUALIFIED, InquiryStatus.CLOSED},
    InquiryStatus.QUALIFIED: {InquiryStatus.QUOTED, InquiryStatus.NEEDS_INFORMATION, InquiryStatus.CLOSED},
    InquiryStatus.QUOTED: {
        InquiryStatus.BOOKED,
        InquiryStatus.QUALIFIED,
        InquiryStatus.NEEDS_INFORMATION,
        InquiryStatus.CLOSED,
    },
    # BOOKED -> QUOTED happens only when the active booking is cancelled (booking service).
    InquiryStatus.BOOKED: {InquiryStatus.COMPLETED, InquiryStatus.QUOTED},
    InquiryStatus.COMPLETED: set(),
    InquiryStatus.CLOSED: {InquiryStatus.QUALIFIED},  # re-open
}
OPEN_INQUIRY_STATUSES = {
    InquiryStatus.NEW,
    InquiryStatus.NEEDS_INFORMATION,
    InquiryStatus.QUALIFIED,
    InquiryStatus.QUOTED,
}
# Statuses that only the booking workflow may set (never the model, never a generic status edit).
BOOKING_CONTROLLED_INQUIRY_STATUSES = {InquiryStatus.BOOKED, InquiryStatus.COMPLETED}
# Statuses the AI agent may *propose* (still requires advisor approval).
MODEL_PROPOSABLE_INQUIRY_STATUSES = {
    InquiryStatus.NEEDS_INFORMATION,
    InquiryStatus.QUALIFIED,
    InquiryStatus.CLOSED,
}


QUOTE_STATUS_LABELS = {
    "received": "Received from operator", "presented": "Sent to customer", "accepted": "Accepted (booked)",
    "declined": "Declined", "expired": "Expired", "withdrawn": "Withdrawn",
}
INQUIRY_STATUS_LABELS = {
    "new": "New", "needs_information": "Waiting for details", "qualified": "Ready to quote", "quoted": "Quote sent",
    "booked": "Booked", "completed": "Trip completed", "closed": "Closed",
}


class QuoteStatus(StrEnum):
    RECEIVED = "received"  # recorded from operator, not yet shared
    PRESENTED = "presented"  # advisor recorded that it was shared with the customer (outside the app)
    ACCEPTED = "accepted"
    DECLINED = "declined"
    EXPIRED = "expired"
    WITHDRAWN = "withdrawn"


QUOTE_TRANSITIONS: dict[QuoteStatus, set[QuoteStatus]] = {
    QuoteStatus.RECEIVED: {QuoteStatus.PRESENTED, QuoteStatus.DECLINED, QuoteStatus.EXPIRED, QuoteStatus.WITHDRAWN},
    QuoteStatus.PRESENTED: {QuoteStatus.ACCEPTED, QuoteStatus.DECLINED, QuoteStatus.EXPIRED, QuoteStatus.WITHDRAWN},
    # ACCEPTED -> PRESENTED only when the resulting booking is cancelled (booking service).
    QuoteStatus.ACCEPTED: {QuoteStatus.PRESENTED},
    QuoteStatus.DECLINED: set(),
    QuoteStatus.EXPIRED: set(),
    QuoteStatus.WITHDRAWN: set(),
}
QUOTE_AWAITING_DECISION = {QuoteStatus.RECEIVED, QuoteStatus.PRESENTED}
QUOTE_COMPARABLE_STATUSES = {QuoteStatus.RECEIVED, QuoteStatus.PRESENTED, QuoteStatus.ACCEPTED}


class BookingStatus(StrEnum):
    CONFIRMED = "confirmed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


BOOKING_TRANSITIONS: dict[BookingStatus, set[BookingStatus]] = {
    BookingStatus.CONFIRMED: {BookingStatus.COMPLETED, BookingStatus.CANCELLED},
    BookingStatus.COMPLETED: set(),
    BookingStatus.CANCELLED: set(),
}
# A booking counts toward "confirmed booking value" if it was confirmed and has not been cancelled.
VALUE_COUNTED_BOOKING_STATUSES = {BookingStatus.CONFIRMED, BookingStatus.COMPLETED}


class FollowUpStatus(StrEnum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    SCHEDULED = "scheduled"
    COMPLETED = "completed"  # advisor recorded that the follow-up happened (outside this prototype)
    CANCELLED = "cancelled"


FOLLOWUP_TRANSITIONS: dict[FollowUpStatus, set[FollowUpStatus]] = {
    # DRAFT/PENDING → COMPLETED lets an owner record "I sent this" without extra approval clicks.
    FollowUpStatus.DRAFT: {FollowUpStatus.PENDING_APPROVAL, FollowUpStatus.APPROVED, FollowUpStatus.COMPLETED,
                           FollowUpStatus.CANCELLED},
    FollowUpStatus.PENDING_APPROVAL: {FollowUpStatus.APPROVED, FollowUpStatus.DRAFT, FollowUpStatus.COMPLETED,
                                      FollowUpStatus.CANCELLED},
    FollowUpStatus.APPROVED: {FollowUpStatus.SCHEDULED, FollowUpStatus.COMPLETED, FollowUpStatus.CANCELLED},
    FollowUpStatus.SCHEDULED: {FollowUpStatus.COMPLETED, FollowUpStatus.CANCELLED},
    FollowUpStatus.COMPLETED: set(),
    FollowUpStatus.CANCELLED: set(),
}
FOLLOWUP_CLOSED_STATUSES = {FollowUpStatus.COMPLETED, FollowUpStatus.CANCELLED}
FOLLOWUP_EDITABLE_STATUSES = {FollowUpStatus.DRAFT, FollowUpStatus.PENDING_APPROVAL}


class FollowUpType(StrEnum):
    INITIAL_RESPONSE = "initial_response"
    MISSING_INFORMATION = "missing_information"
    QUOTE_FOLLOWUP = "quote_followup"
    ALTERNATIVES = "alternatives"
    ACCOMMODATION = "accommodation"
    ADDITIONAL_SERVICES = "additional_services"
    POST_INTERACTION = "post_interaction"
    POST_TRIP = "post_trip"
    BOOKING_CONFIRMATION = "booking_confirmation"
    GENERAL_UPDATE = "general_update"


FOLLOWUP_TYPE_LABELS = {
    FollowUpType.INITIAL_RESPONSE: "Initial response",
    FollowUpType.MISSING_INFORMATION: "Request missing information",
    FollowUpType.QUOTE_FOLLOWUP: "Follow up on a shared quote",
    FollowUpType.ALTERNATIVES: "Offer documented alternatives",
    FollowUpType.ACCOMMODATION: "Coordinate requested accommodation",
    FollowUpType.ADDITIONAL_SERVICES: "Check additional services",
    FollowUpType.POST_INTERACTION: "Follow up after an interaction",
    FollowUpType.POST_TRIP: "Post-trip follow-up",
    FollowUpType.BOOKING_CONFIRMATION: "Booking confirmation (draft)",
    FollowUpType.GENERAL_UPDATE: "General customer update",
}
# The email types offered first in the UI (others remain available under "More").
PRIMARY_EMAIL_TYPES = [FollowUpType.INITIAL_RESPONSE, FollowUpType.MISSING_INFORMATION, FollowUpType.QUOTE_FOLLOWUP,
                       FollowUpType.BOOKING_CONFIRMATION, FollowUpType.GENERAL_UPDATE]


class Tone(StrEnum):
    PROFESSIONAL = "professional"
    WARM = "warm"
    CONCISE = "concise"


class RecommendationStatus(StrEnum):
    OPEN = "open"
    ACCEPTED = "accepted"
    DISMISSED = "dismissed"
    IRRELEVANT = "irrelevant"


RECOMMENDATION_DECISIONS = {RecommendationStatus.ACCEPTED, RecommendationStatus.DISMISSED, RecommendationStatus.IRRELEVANT}


class ServiceCategory(StrEnum):
    CHARTER_FLIGHT = "charter_flight"
    EMPTY_LEG = "empty_leg"
    ACCOMMODATION = "accommodation"
    GROUND_TRANSPORT = "ground_transport"
    CATERING = "catering"
    PET_TRAVEL = "pet_travel"
    SPECIAL_LUGGAGE = "special_luggage"
    EXPERIENCES = "experiences"


# Additional services a customer can request in an inquiry (charter itself is implicit).
REQUESTABLE_SERVICES = [
    ServiceCategory.ACCOMMODATION,
    ServiceCategory.GROUND_TRANSPORT,
    ServiceCategory.CATERING,
    ServiceCategory.PET_TRAVEL,
    ServiceCategory.SPECIAL_LUGGAGE,
    ServiceCategory.EXPERIENCES,
]

SUPPORTED_CURRENCIES = ["USD", "EUR", "GBP", "CHF", "CAD", "AUD", "AED", "SGD", "JPY"]

TRIP_TYPES = ["one_way", "round_trip", "multi_leg"]
TRIP_PURPOSES = ["business", "leisure", "event", "family", "personal"]
DATE_FLEXIBILITY = ["flexible", "fixed"]


def ensure_transition(table: dict, current: StrEnum, target: StrEnum, entity: str) -> None:
    """Raise :class:`InvalidTransition` unless ``current -> target`` is permitted by ``table``."""
    if current == target:
        raise InvalidTransition(f"{entity} is already '{current}'.")
    allowed = table.get(current, set())
    if target not in allowed:
        allowed_txt = ", ".join(sorted(str(s) for s in allowed)) or "none (terminal state)"
        raise InvalidTransition(f"{entity} cannot move from '{current}' to '{target}'. Allowed: {allowed_txt}.")
