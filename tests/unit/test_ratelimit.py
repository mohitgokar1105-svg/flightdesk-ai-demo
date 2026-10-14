from src.ratelimit import RateLimiter, client_key


def test_blocks_after_limit_and_recovers_after_window():
    rl = RateLimiter(limit=3, window_seconds=60)
    assert all(rl.allow("a", now=t) for t in (0, 1, 2))
    assert not rl.allow("a", now=3)
    assert not rl.allow("a", now=59)
    assert rl.allow("a", now=61)  # first event has aged out


def test_keys_are_independent():
    rl = RateLimiter(limit=1, window_seconds=60)
    assert rl.allow("a", now=0)
    assert rl.allow("b", now=0)
    assert not rl.allow("a", now=1)


def test_forwarded_header_ignored_unless_proxy_trusted(monkeypatch):
    headers = {"X-Forwarded-For": "6.6.6.6, 203.0.113.9"}
    monkeypatch.delenv("TRUST_PROXY_HEADERS", raising=False)
    assert client_key(headers) == "direct"
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "1")
    assert client_key(headers) == "203.0.113.9"  # last entry = appended by our proxy, not client-controlled
    assert client_key({}) == "direct"
