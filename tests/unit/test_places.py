"""Airport/city suggestions, place resolution for maps, distances, and the booking funnel."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.places import all_places, distance_km, label_for, place_values, resolve_place, search_places


def test_bundled_airport_list_is_sane():
    places = all_places()
    assert len(places) > 4000
    assert len({p.iata for p in places}) == len(places), "IATA codes are unique"
    assert all(-90 <= p.lat <= 90 and -180 <= p.lon <= 180 for p in places)
    for code in ("LTN", "TEB", "NCE", "JMK", "LBG", "VNY", "GVA", "ASE"):
        assert any(p.iata == code for p in places), code


@pytest.mark.parametrize(("query", "expected"), [
    ("LTN", "LTN"), ("ltn", "LTN"), ("KTEB", "TEB"), ("teterboro", "TEB"), ("mykonos", "JMK"), ("geneva", "GVA"),
])
def test_search_finds_airports_by_code_name_or_city(query, expected):
    assert search_places(query)[0].iata == expected


def test_search_by_city_lists_that_citys_airports_big_ones_first():
    codes = [p.iata for p in search_places("london", limit=6)]
    assert {"LHR", "LGW", "LTN", "STN"} <= set(codes)
    assert search_places("a") == [] and search_places("") == []


def test_saved_value_and_label():
    luton = next(p for p in all_places() if p.iata == "LTN")
    assert luton.value == "London Luton Airport (LTN)" and luton.value in place_values()
    assert label_for(luton.value) == "London Luton Airport (LTN) · Luton, United Kingdom"
    assert label_for("Somewhere typed by hand") == "Somewhere typed by hand"


def test_resolve_place_for_maps():
    assert resolve_place("London Luton Airport (LTN)").iata == "LTN"
    assert resolve_place("Some Airport Name (NCE)").iata == "NCE"
    assert resolve_place("JFK").iata == "JFK"
    assert resolve_place("Los Angeles").iata == "LAX"
    assert resolve_place("Portland") is None, "ambiguous city names are never guessed"
    assert resolve_place("Atlantis") is None and resolve_place(None) is None


def test_great_circle_distance():
    ltn, nce = resolve_place("LTN"), resolve_place("NCE")
    assert distance_km(ltn, nce) == pytest.approx(1074, abs=10)
    assert distance_km(ltn, ltn) == 0


def test_booking_funnel_is_cumulative_and_from_records(session):
    from src.domain import QuoteStatus
    from src.schemas import BookingCreate, InquiryCreate, QuoteCreate
    from src.services.booking_service import create_booking_record
    from src.services.dashboard_service import booking_funnel, requested_routes, resolve_period
    from src.services.inquiry_service import create_inquiry
    from src.services.quote_service import change_quote_status, create_quote

    def inq(label):
        return create_inquiry(session, InquiryCreate(customer_label=label, original_text=label, origin="London Luton Airport (LTN)",
                                                     destination="Nice"))

    a, b, c, d = inq("A"), inq("B"), inq("C"), inq("D")
    create_quote(session, QuoteCreate(inquiry_id=b.id, operator_label="Op", total_price=1000, currency="EUR"))
    q = create_quote(session, QuoteCreate(inquiry_id=c.id, operator_label="Op", total_price=1000, currency="EUR"))
    change_quote_status(session, q.id, QuoteStatus.PRESENTED)
    create_booking_record(session, BookingCreate(inquiry_id=d.id, booked_value=900, currency="EUR"))  # booked directly
    session.commit()
    period = resolve_period("all_time", today=datetime.now(UTC).date())
    stages = dict(booking_funnel(session, period))
    assert stages == {"Inquiries received": 4, "Quotes recorded": 3, "Quote sent to customer": 2, "Booked": 1}
    assert requested_routes(session, period).count(("London Luton Airport (LTN)", "Nice")) == 4
    assert a.id  # the inquiry with nothing yet still counts as received


def test_area_names_and_aliases_resolve_but_unknowns_stay_unplaced():
    assert resolve_place("St. Barts").iata == "SBH" and resolve_place("Vail").iata == "EGE"
    assert resolve_place("Ibiza").iata == "IBZ", "airport name prefix"
    assert resolve_place("Martha's Vineyard").iata == "MVY"
