"""Shared, thread-safe rate limiting + retry for external services.

The stack is synchronous (ytmusicapi / pylast / musicbrainzngs are all sync), so
we use a thread-safe token bucket rather than the async ``aiolimiter``. Limiters
are module-level singletons keyed by service name — rate limits are per-IP/global,
so every client instance must share the same bucket.
"""

from __future__ import annotations

import threading
import time

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

# Conservative per-service rates (requests/second). Deliberately below the
# documented ceilings — better a touch slow than banned.
_RATES: dict[str, float] = {
    "musicbrainz": 1.0,        # MB mandates ≤1 req/s/IP
    "acousticbrainz": 1.0,     # ~10/10s ceiling; we stay well under
    "lastfm": 3.0,             # "several/sec" risks suspension; keep modest
    "listenbrainz": 0.8,       # ~48/min, under the 50/min read budget
    "listenbrainz_labs": 3.0,  # experimental; no documented limit
    "ytm_read": 4.0,
    "ytm_write": 0.5,          # ~1 op / 2s — mirrors --track-sleep guidance
}


class TokenBucket:
    """Classic token bucket. ``acquire()`` blocks until a token is available."""

    def __init__(self, rate_per_sec: float, capacity: float | None = None):
        self.rate = rate_per_sec
        self.capacity = capacity if capacity is not None else max(1.0, rate_per_sec)
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self.capacity, self._tokens + (now - self._updated) * self.rate
                )
                self._updated = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                wait = (tokens - self._tokens) / self.rate
            time.sleep(wait)


_LIMITERS: dict[str, TokenBucket] = {}
_REGISTRY_LOCK = threading.Lock()


def limiter(name: str) -> TokenBucket:
    """Fetch (or lazily create) the shared limiter for a service."""
    with _REGISTRY_LOCK:
        bucket = _LIMITERS.get(name)
        if bucket is None:
            bucket = TokenBucket(_RATES.get(name, 2.0))
            _LIMITERS[name] = bucket
        return bucket


def rated_get(client, limiter_name: str, path: str, *, params=None, max_retries: int = 4):
    """Rate-limited GET that honours 429 / X-RateLimit / Retry-After and retries 5xx.

    Returns the final ``httpx.Response`` (including terminal 4xx for the caller to
    interpret). ``client`` is any object with a ``.get(path, params=...)`` method.
    """
    import httpx

    bucket = limiter(limiter_name)
    last = None
    for attempt in range(max_retries):
        bucket.acquire()
        try:
            r = client.get(path, params=params)
        except httpx.TransportError:
            # Timeouts, connection resets, etc. — transient; back off and retry.
            if attempt == max_retries - 1:
                raise
            time.sleep(min(2 * (attempt + 1), 30))
            continue
        last = r
        if r.status_code == 429:
            wait = float(r.headers.get("X-RateLimit-Reset-In", r.headers.get("Retry-After", 10)))
            time.sleep(min(wait + 0.5, 60))
            continue
        if r.status_code >= 500:
            time.sleep(min(2 * (attempt + 1), 30))
            continue
        # Proactive cool-down when the window is nearly spent.
        remaining = r.headers.get("X-RateLimit-Remaining")
        if remaining is not None and remaining.isdigit() and int(remaining) <= 2:
            time.sleep(float(r.headers.get("X-RateLimit-Reset-In", 5)))
        return r
    return last


def transient_retry(exc_types: type | tuple[type, ...], attempts: int = 4):
    """Decorator: retry on the given transient exception types with jitter."""
    return retry(
        reraise=True,
        stop=stop_after_attempt(attempts),
        wait=wait_exponential_jitter(initial=1, max=30),
        retry=retry_if_exception_type(exc_types),
    )
