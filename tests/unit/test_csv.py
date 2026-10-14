"""CSV export and validated import (preview, invalid rows, duplicates, overwrite confirmation, transactions)."""

from __future__ import annotations

import pandas as pd
import pytest

from src.config import SAMPLES_DIR
from src.exceptions import ValidationFailure
from src.models import Inquiry, Quote
from src.services.csv_service import commit_import, export_entity, read_csv_upload, validate_import


def test_sample_inquiries_preview_and_import(session):
    df = read_csv_upload((SAMPLES_DIR / "sample_inquiries.csv").read_bytes())
    results = validate_import(session, "inquiries", df)
    statuses = [r.status for r in results]
    assert statuses.count("valid") == 3 and statuses.count("invalid") == 3 and statuses.count("duplicate") == 1
    reasons = " ".join(r.reason for r in results if r.status == "invalid").lower()
    assert "passenger_count" in reasons and "currency" in reasons and "return date" in reasons
    summary = commit_import(session, "inquiries", results)
    assert summary["inserted"] == 3 and summary["skipped_invalid"] == 3 and summary["skipped_duplicates"] == 1
    again = validate_import(session, "inquiries", df)
    assert all(r.status in ("duplicate", "invalid") for r in again), "re-import creates no duplicates"


def test_sample_quotes_import_by_customer_label(session):
    commit_import(session, "inquiries", validate_import(session, "inquiries", read_csv_upload((SAMPLES_DIR / "sample_inquiries.csv").read_bytes())))
    results = validate_import(session, "quotes", read_csv_upload((SAMPLES_DIR / "sample_quotes.csv").read_bytes()))
    assert [r.status for r in results].count("valid") == 3 and [r.status for r in results].count("invalid") == 2
    commit_import(session, "quotes", results)
    assert session.query(Quote).count() == 3


def test_overwrite_requires_explicit_confirmation(session):
    commit_import(session, "inquiries", validate_import(session, "inquiries", read_csv_upload((SAMPLES_DIR / "sample_inquiries.csv").read_bytes())))
    inq = session.query(Inquiry).filter(Inquiry.customer_label == "Demo Import Customer 002").one()
    df = pd.DataFrame([{"inquiry_id": inq.id, "customer_label": inq.customer_label, "original_text": "changed",
                        "origin": "Teterboro", "destination": "Nantucket", "passenger_count": "9"}])
    results = validate_import(session, "inquiries", df)
    assert results[0].status == "update"
    commit_import(session, "inquiries", results, overwrite=False)
    assert session.get(Inquiry, inq.id).passenger_count == 3
    commit_import(session, "inquiries", results, overwrite=True)
    assert session.get(Inquiry, inq.id).passenger_count == 9


def test_unknown_columns_and_bad_csv_rejected(session):
    with pytest.raises(ValidationFailure):
        validate_import(session, "inquiries", pd.DataFrame([{"customer_label": "x", "secret_sql": "DROP"}]))
    with pytest.raises(ValidationFailure):
        validate_import(session, "bookings", pd.DataFrame([{"a": 1}]))


def test_export_writes_csv(seeded, tmp_path):
    for entity in ("inquiries", "quotes", "bookings", "followups", "recommendations"):
        path, df = export_entity(seeded, entity, directory=tmp_path)
        assert path.exists() and len(pd.read_csv(path)) == len(df) > 0
