"""AcousticBrainz client.

The project froze submissions in June 2022, but the website, API and CC0 data
remain live and queryable. Coverage is hit-or-miss (≈half of typical libraries),
so treat this as enrichment, not a guarantee.

Bulk endpoints take up to 25 MBIDs (semicolon-separated). Rate-limit headers are
honoured: HTTP 429 carries ``X-RateLimit-Reset-In`` which doubles as Retry-After.
"""

from __future__ import annotations

import time
from collections.abc import Iterable

import httpx

from merlin.config import USER_AGENT, Settings, get_settings

BASE = "https://acousticbrainz.org/api/v1"
BATCH = 25
MAX_RETRIES = 4


def _chunks(items: list[str], n: int) -> Iterable[list[str]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


class AcousticBrainzClient:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._client = httpx.Client(
            base_url=BASE,
            headers={"User-Agent": USER_AGENT},
            timeout=30,
            http2=True,
        )

    def _get(self, path: str, params: dict) -> httpx.Response | None:
        for attempt in range(MAX_RETRIES):
            r = self._client.get(path, params=params)
            if r.status_code == 429:
                wait = float(r.headers.get("X-RateLimit-Reset-In", 10))
                time.sleep(min(wait + 0.5, 30))
                continue
            if r.status_code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 200:
                # Stay polite when the window is nearly spent.
                remaining = int(r.headers.get("X-RateLimit-Remaining", 100))
                if remaining <= 2:
                    time.sleep(float(r.headers.get("X-RateLimit-Reset-In", 10)))
                return r
            return None
        return None

    def _bulk(self, level: str, mbids: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for batch in _chunks(list(dict.fromkeys(mbids)), BATCH):
            r = self._get(f"/{level}", params={"recording_ids": ";".join(batch)})
            if r is None:
                continue
            for mbid, submissions in r.json().items():
                if mbid == "mbid_mapping" or not submissions:
                    continue
                first = submissions[sorted(submissions.keys())[0]]
                out[mbid] = first
        return out

    def low_level_bulk(self, mbids: list[str]) -> dict[str, dict]:
        return self._bulk("low-level", mbids)

    def high_level_bulk(self, mbids: list[str]) -> dict[str, dict]:
        return self._bulk("high-level", mbids)

    def close(self) -> None:
        self._client.close()
