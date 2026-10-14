"""Application clock. Normally real UTC time; evaluations freeze it so fixtures and "this month" are deterministic."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

_frozen: ContextVar[datetime | None] = ContextVar("flightdesk_frozen_now", default=None)


def now() -> datetime:
    frozen = _frozen.get()
    return frozen if frozen is not None else datetime.now(UTC)


@contextmanager
def frozen_time(moment: datetime) -> Iterator[datetime]:
    if moment.tzinfo is None:
        raise ValueError("frozen_time needs a timezone-aware datetime")
    token = _frozen.set(moment)
    try:
        yield moment
    finally:
        _frozen.reset(token)
