"""Tests for the shared rate-limit + retry plumbing. No network."""

from __future__ import annotations

import merlin.ratelimit as rl
from merlin.ratelimit import TokenBucket, limiter, rated_get


class FakeResp:
    def __init__(self, status, headers=None, body=None):
        self.status_code = status
        self.headers = headers or {}
        self._body = body

    def json(self):
        return self._body


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, path, params=None):
        r = self.responses[self.calls]
        self.calls += 1
        if isinstance(r, Exception):
            raise r
        return r


def test_token_bucket_bursts_then_throttles(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(rl.time, "sleep", lambda s: slept.append(s))
    b = TokenBucket(rate_per_sec=100.0, capacity=2.0)
    b.acquire()
    b.acquire()
    assert slept == []  # capacity covers the first two
    b.acquire()
    assert slept and slept[0] > 0  # third must wait for a refill


def test_limiter_is_singleton_per_name():
    assert limiter("musicbrainz") is limiter("musicbrainz")
    assert limiter("musicbrainz") is not limiter("lastfm")


def test_rated_get_retries_429_then_succeeds(monkeypatch):
    monkeypatch.setattr(rl.time, "sleep", lambda s: None)
    client = FakeClient(
        [
            FakeResp(429, {"X-RateLimit-Reset-In": "0"}),
            FakeResp(200, {}, {"ok": True}),
        ]
    )
    r = rated_get(client, "test_429", "/x")
    assert r.status_code == 200 and client.calls == 2


def test_rated_get_retries_5xx(monkeypatch):
    monkeypatch.setattr(rl.time, "sleep", lambda s: None)
    client = FakeClient([FakeResp(503), FakeResp(503), FakeResp(200, {}, {})])
    r = rated_get(client, "test_5xx", "/x")
    assert r.status_code == 200 and client.calls == 3


def test_rated_get_returns_terminal_4xx(monkeypatch):
    monkeypatch.setattr(rl.time, "sleep", lambda s: None)
    client = FakeClient([FakeResp(401, {}, {"error": "nope"})])
    r = rated_get(client, "test_401", "/x")
    assert r.status_code == 401 and client.calls == 1  # no retry on terminal 4xx


def test_rated_get_honours_low_remaining(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(rl.time, "sleep", lambda s: slept.append(s))
    client = FakeClient(
        [FakeResp(200, {"X-RateLimit-Remaining": "1", "X-RateLimit-Reset-In": "3"}, {})]
    )
    rated_get(client, "test_cool", "/x")
    assert 3.0 in slept  # proactive cool-down when window nearly spent


def test_rated_get_retries_transport_error(monkeypatch):
    import httpx

    monkeypatch.setattr(rl.time, "sleep", lambda s: None)
    client = FakeClient([httpx.ReadTimeout("boom"), FakeResp(200, {}, {"ok": True})])
    r = rated_get(client, "test_timeout", "/x")
    assert r.status_code == 200 and client.calls == 2


def test_rated_get_reraises_persistent_transport_error(monkeypatch):
    import httpx
    import pytest

    monkeypatch.setattr(rl.time, "sleep", lambda s: None)
    client = FakeClient([httpx.ConnectError("x")] * 4)
    with pytest.raises(httpx.TransportError):
        rated_get(client, "test_timeout2", "/x", max_retries=4)


def test_transient_retry_decorator(monkeypatch):
    import tenacity.nap

    monkeypatch.setattr(tenacity.nap.time, "sleep", lambda s: None)
    calls = {"n": 0}

    @rl.transient_retry(ValueError, attempts=3)
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ValueError("blip")
        return "ok"

    assert flaky() == "ok" and calls["n"] == 3
