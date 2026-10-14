"""Server-side sliding-window rate limiting for the public customer pages.

State is held in this process's memory (shared by every browser session), so it is not defeated by opening a new tab or
clearing cookies. With several app processes each keeps its own counters; run one process or add a shared limit at the
reverse proxy as well.
"""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque

_MAX_TRACKED_KEYS = 10_000


class RateLimiter:
    """Allow at most ``limit`` events per ``window_seconds`` for each key."""

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        """Record an attempt for ``key``; return False (and record nothing) when the limit is already reached."""
        t = time.monotonic() if now is None else now
        with self._lock:
            if len(self._events) > _MAX_TRACKED_KEYS:
                self._prune(t)
            q = self._events[key]
            while q and t - q[0] >= self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(t)
            return True

    def _prune(self, t: float) -> None:
        for k in [k for k, q in self._events.items() if not q or t - q[-1] >= self.window]:
            del self._events[k]

    def reset(self) -> None:
        with self._lock:
            self._events.clear()


def client_key(headers: dict[str, str] | None) -> str:
    """Identify the caller for rate limiting.

    ``X-Forwarded-For`` is only honoured when ``TRUST_PROXY_HEADERS=1`` (the app is behind our reverse proxy, which
    appends the real client address as the *last* entry); otherwise a spoofed header could dodge the limit.
    """
    if headers and os.getenv("TRUST_PROXY_HEADERS") == "1":
        forwarded = {k.lower(): v for k, v in headers.items()}.get("x-forwarded-for", "")
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return "direct"


# Customer status checks: guessing reference + email pairs. Flight requests: spam / flooding the team's inbox.
STATUS_LOOKUPS = RateLimiter(limit=10, window_seconds=15 * 60)
FLIGHT_REQUESTS = RateLimiter(limit=5, window_seconds=60 * 60)
