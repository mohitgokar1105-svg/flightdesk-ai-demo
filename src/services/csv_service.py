"""CSV export and validated CSV import. SQLite remains the source of truth.

Import flow: :func:`validate_import` → preview (valid / invalid / duplicate / update rows with reasons) →
:func:`commit_import` inside one transaction. Existing records are only overwritten when the caller passes
``overwrite=True`` *and* the row names an existing record ID.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.auth import Permission, require
from src.config import EXPORT_DIR
from src.exceptions import ValidationFailure
from src.models import Booking, FollowUp, Inquiry, Quote, Recommendation, utcnow
from src.schemas import InquiryCreate, QuoteCreate
from src.services.audit_service import record_audit
from src.services.booking_service import booking_to_dict
from src.services.followup_service import followup_to_dict
from src.services.inquiry_service import (
    INQUIRY_FIELDS,
    compute_content_hash,
    inquiry_to_dict,
    missing_information_for,
)
from src.services.quote_service import quote_dedupe_key, quote_to_dict
from src.services.recommendation_service import recommendation_to_dict

EXPORTABLE = ("inquiries", "quotes", "bookings", "followups", "recommendations")
IMPORTABLE = ("inquiries", "quotes")
MAX_IMPORT_ROWS = 2000
LIST_SEPARATOR = ";"

INQUIRY_IMPORT_COLUMNS = ["inquiry_id", "customer_label", "customer_email", "customer_phone", "original_text", *INQUIRY_FIELDS]
QUOTE_IMPORT_COLUMNS = ["quote_id", "inquiry_id", "inquiry_customer_label", "operator_label", "total_price", "currency",
                        "aircraft_type", "passenger_capacity", "included_services", "excluded_services", "restrictions",
                        "valid_until", "notes", "source_description"]


def entity_frame(session: Session, entity: str) -> pd.DataFrame:
    require(Permission.EXPORT_DATA)
    if entity == "inquiries":
        rows = [inquiry_to_dict(i, include_text=True) for i in session.scalars(select(Inquiry).order_by(Inquiry.created_at))]
    elif entity == "quotes":
        rows = [quote_to_dict(q) for q in session.scalars(select(Quote).order_by(Quote.created_at))]
    elif entity == "bookings":
        rows = [booking_to_dict(b) for b in session.scalars(select(Booking).order_by(Booking.created_at))]
    elif entity == "followups":
        rows = [followup_to_dict(f) for f in session.scalars(select(FollowUp).order_by(FollowUp.created_at))]
    elif entity == "recommendations":
        rows = [recommendation_to_dict(r) for r in session.scalars(select(Recommendation).order_by(Recommendation.created_at))]
    else:
        raise ValidationFailure(f"Unknown export entity '{entity}'.")
    df = pd.DataFrame(rows)
    return df.map(_cell) if not df.empty else df


def _cell(v: Any) -> Any:
    """Flatten list/dict cells: simple lists → 'a;b', nested structures → JSON text."""
    if isinstance(v, list) and all(not isinstance(x, list | dict) for x in v):
        return LIST_SEPARATOR.join(map(str, v))
    if isinstance(v, list | dict):
        return json.dumps(v, default=str)
    return v


def export_entity(session: Session, entity: str, directory: Path = EXPORT_DIR) -> tuple[Path, pd.DataFrame]:
    """Write ``entity`` to ``data/exports/<entity>_<UTC timestamp>.csv`` and return the path + frame."""
    require(Permission.EXPORT_DATA)
    if entity not in EXPORTABLE:
        raise ValidationFailure(f"'{entity}' cannot be exported.")
    df = entity_frame(session, entity)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{entity}_{utcnow().strftime('%Y%m%dT%H%M%SZ')}.csv"
    df.to_csv(path, index=False)
    record_audit(session, "csv_export", entity_type=entity, metadata={"rows": len(df), "file": path.name})
    return path, df


def frame_to_csv_bytes(df: pd.DataFrame) -> bytes:
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    return buf.getvalue().encode("utf-8")


# ---------------------------------------------------------------------------- import
@dataclass
class RowResult:
    row_number: int  # 1-based data row (header excluded)
    status: str  # valid | update | duplicate | invalid
    reason: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    target_id: str | None = None


def _clean(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def _split(value: Any) -> list[str]:
    value = _clean(value)
    if value is None:
        return []
    return [p.strip() for p in str(value).replace(",", LIST_SEPARATOR).split(LIST_SEPARATOR) if p.strip()]


def read_csv_upload(data: bytes) -> pd.DataFrame:
    try:
        df = pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False)
    except Exception as exc:  # malformed CSV
        raise ValidationFailure(f"Could not read CSV: {str(exc)[:200]}") from exc
    if len(df) > MAX_IMPORT_ROWS:
        raise ValidationFailure(f"CSV has {len(df)} rows; the maximum per import is {MAX_IMPORT_ROWS}.")
    df.columns = [c.strip().lower() for c in df.columns]
    return df


def _pydantic_reason(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, e['loc'])) or 'row'}: {e['msg']}" for e in exc.errors()[:4])


def validate_import(session: Session, entity: str, df: pd.DataFrame) -> list[RowResult]:
    require(Permission.ADMIN_DATA)
    if entity not in IMPORTABLE:
        raise ValidationFailure(f"'{entity}' cannot be imported.")
    expected = INQUIRY_IMPORT_COLUMNS if entity == "inquiries" else QUOTE_IMPORT_COLUMNS
    unknown = [c for c in df.columns if c not in expected]
    if unknown:
        raise ValidationFailure(f"Unknown column(s): {', '.join(unknown)}. Expected a subset of: {', '.join(expected)}")
    results: list[RowResult] = []
    seen_keys: set[str] = set()
    for idx, raw in enumerate(df.to_dict(orient="records"), start=1):
        row = {k: _clean(v) for k, v in raw.items()}
        try:
            if entity == "inquiries":
                results.append(_validate_inquiry_row(session, idx, row, seen_keys))
            else:
                results.append(_validate_quote_row(session, idx, row, seen_keys))
        except ValidationError as exc:
            results.append(RowResult(idx, "invalid", _pydantic_reason(exc)))
        except ValidationFailure as exc:
            results.append(RowResult(idx, "invalid", str(exc)))
    return results


def _validate_inquiry_row(session: Session, idx: int, row: dict[str, Any], seen: set[str]) -> RowResult:
    target_id = row.pop("inquiry_id", None)
    row["requested_services"] = _split(row.get("requested_services"))
    data = InquiryCreate.model_validate({k: v for k, v in row.items() if v is not None or k == "requested_services"})
    key = compute_content_hash(data.customer_label, data.original_text, data)
    if key in seen:
        return RowResult(idx, "duplicate", "Duplicate of an earlier row in this file.")
    seen.add(key)
    payload = data.model_dump(mode="json")
    if target_id:
        existing = session.get(Inquiry, target_id)
        if existing is None:
            return RowResult(idx, "invalid", f"inquiry_id {target_id} does not exist (leave blank to create a new inquiry).")
        return RowResult(idx, "update", f"Would overwrite {target_id} (only if overwrite is confirmed).", payload, target_id)
    if session.scalar(select(Inquiry.id).where(Inquiry.content_hash == key)):
        return RowResult(idx, "duplicate", "An identical inquiry already exists; skipped.")
    return RowResult(idx, "valid", "", payload)


def _validate_quote_row(session: Session, idx: int, row: dict[str, Any], seen: set[str]) -> RowResult:
    target_id = row.pop("quote_id", None)
    label = row.pop("inquiry_customer_label", None)
    if not row.get("inquiry_id") and label:
        matches = list(session.scalars(select(Inquiry.id).where(Inquiry.customer_label == label)))
        if len(matches) != 1:
            raise ValidationFailure(f"inquiry_customer_label '{label}' matches {len(matches)} inquiries; provide inquiry_id.")
        row["inquiry_id"] = matches[0]
    if not row.get("inquiry_id"):
        raise ValidationFailure("inquiry_id (or a unique inquiry_customer_label) is required.")
    inquiry = session.get(Inquiry, row["inquiry_id"])
    if inquiry is None:
        raise ValidationFailure(f"Inquiry {row['inquiry_id']} does not exist.")
    row["included_services"] = _split(row.get("included_services"))
    row["excluded_services"] = _split(row.get("excluded_services"))
    data = QuoteCreate.model_validate({k: v for k, v in row.items() if v is not None or k.endswith("_services")})
    key = quote_dedupe_key(data)
    if key in seen:
        return RowResult(idx, "duplicate", "Duplicate of an earlier row in this file.")
    seen.add(key)
    payload = data.model_dump(mode="json")
    if target_id:
        if session.get(Quote, target_id) is None:
            return RowResult(idx, "invalid", f"quote_id {target_id} does not exist (leave blank to create a new quote).")
        return RowResult(idx, "update", f"Would overwrite {target_id} (only if overwrite is confirmed).", payload, target_id)
    if session.scalar(select(Quote.id).where(Quote.dedupe_key == key)):
        return RowResult(idx, "duplicate", "An identical quote already exists; skipped.")
    if inquiry.status in ("completed", "closed"):
        return RowResult(idx, "invalid", f"Inquiry {inquiry.id} is {inquiry.status}; quotes cannot be added.")
    return RowResult(idx, "valid", "", payload)


def preview_frame(results: list[RowResult]) -> pd.DataFrame:
    return pd.DataFrame([{"row": r.row_number, "status": r.status, "reason": r.reason, "target_id": r.target_id or ""}
                         for r in results])


def commit_import(session: Session, entity: str, results: list[RowResult], *, overwrite: bool = False) -> dict[str, int]:
    """Insert valid rows (and updates if ``overwrite``). Runs in the caller's transaction: any error rolls back all rows."""
    require(Permission.ADMIN_DATA)
    from src.services.inquiry_service import create_inquiry
    from src.services.quote_service import create_quote

    summary = {"inserted": 0, "updated": 0, "skipped_duplicates": 0, "skipped_invalid": 0, "skipped_updates": 0}
    for r in results:
        if r.status == "invalid":
            summary["skipped_invalid"] += 1
        elif r.status == "duplicate":
            summary["skipped_duplicates"] += 1
        elif r.status == "update" and not overwrite:
            summary["skipped_updates"] += 1
        elif entity == "inquiries":
            data = InquiryCreate.model_validate(r.payload)
            if r.status == "update":
                inq = session.get(Inquiry, r.target_id)
                assert inq is not None
                for f in INQUIRY_FIELDS:
                    setattr(inq, f, getattr(data, f))
                inq.customer_label, inq.original_text = data.customer_label, data.original_text
                inq.content_hash = compute_content_hash(data.customer_label, data.original_text, data)
                inq.missing_information = missing_information_for(data)
                inq.updated_at = utcnow()
                record_audit(session, "inquiry_overwritten_by_import", entity_type="inquiry", entity_id=inq.id)
                summary["updated"] += 1
            else:
                create_inquiry(session, data, source="csv_import")
                summary["inserted"] += 1
        else:
            data_q = QuoteCreate.model_validate(r.payload)
            if r.status == "update":
                q = session.get(Quote, r.target_id)
                assert q is not None
                for k, v in data_q.model_dump().items():
                    setattr(q, k, v)
                q.dedupe_key = quote_dedupe_key(data_q)
                q.updated_at = utcnow()
                record_audit(session, "quote_overwritten_by_import", entity_type="quote", entity_id=q.id)
                summary["updated"] += 1
            else:
                create_quote(session, data_q)
                summary["inserted"] += 1
    session.flush()
    record_audit(session, "csv_import", entity_type=entity, metadata=summary)
    return summary
