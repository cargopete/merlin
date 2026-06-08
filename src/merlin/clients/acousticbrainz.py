"""AcousticBrainz client.

The project froze submissions in June 2022, but the website, API and CC0 data
remain live and queryable. Coverage is hit-or-miss (≈half of typical libraries),
so treat this as enrichment, not a guarantee.

Bulk endpoints take up to 25 MBIDs (semicolon-separated). Rate-limit headers are
honoured: HTTP 429 carries ``X-RateLimit-Reset-In`` which doubles as Retry-After.
"""

from __future__ import annotations

from collections.abc import Iterable

import httpx

from merlin.config import USER_AGENT, Settings, get_settings
from merlin.ratelimit import rated_get

BASE = "https://acousticbrainz.org/api/v1"
BATCH = 25


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

    def _bulk(self, level: str, mbids: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for batch in _chunks(list(dict.fromkeys(mbids)), BATCH):
            r = rated_get(
                self._client,
                "acousticbrainz",
                f"/{level}",
                params={"recording_ids": ";".join(batch)},
            )
            if r is None or r.status_code != 200:
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
