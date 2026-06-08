"""MusicBrainz client — the canonical ID hub.

Every track in MB has a recording MBID; that MBID is the only stable identifier
across AcousticBrainz, ListenBrainz, Last.fm and the research datasets. We resolve
ISRC-first (one hop, exact) and fall back to a scored title/artist search.

musicbrainzngs enforces the mandatory 1 req/s/IP limit for us. A meaningful
User-Agent is non-negotiable — MB blocks anonymous/abusive callers.
"""

from __future__ import annotations

import musicbrainzngs

from merlin.config import Settings, get_settings
from merlin.ratelimit import transient_retry

_CONFIGURED = False

# musicbrainzngs already enforces ≤1 req/s; this only adds resilience to
# transient network blips (it does NOT retry ResponseError / 4xx).
@transient_retry(musicbrainzngs.NetworkError, attempts=3)
def _retry(fn):
    return fn()


def _ensure_configured(settings: Settings) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    musicbrainzngs.set_useragent("merlin", "0.1", settings.contact_email)
    musicbrainzngs.set_rate_limit(limit_or_interval=1.0, new_requests=1)
    _CONFIGURED = True


def _artists(rec: dict) -> list[str]:
    out: list[str] = []
    for credit in rec.get("artist-credit", []):
        if isinstance(credit, dict) and "artist" in credit:
            name = credit["artist"].get("name")
            if name:
                out.append(name)
    return out


def _duration_ms(rec: dict) -> int | None:
    length = rec.get("length")
    return int(length) if length else None


class MBRecording:
    __slots__ = ("mbid", "title", "artists", "duration_ms", "isrc", "score")

    def __init__(self, rec: dict, score: float | None = None):
        self.mbid: str = rec["id"]
        self.title: str = rec.get("title", "")
        self.artists: list[str] = _artists(rec)
        self.duration_ms: int | None = _duration_ms(rec)
        self.isrc: str | None = (rec.get("isrc-list") or [None])[0]
        # ext:score is a string percentage 0..100
        raw = rec.get("ext:score") if score is None else score
        self.score: float = float(raw) / 100.0 if raw is not None else 0.0


class MusicBrainzClient:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        _ensure_configured(self.settings)

    def by_isrc(self, isrc: str) -> list[MBRecording]:
        try:
            res = _retry(
                lambda: musicbrainzngs.get_recordings_by_isrc(isrc, includes=["artists"])
            )
        except musicbrainzngs.ResponseError:
            return []
        recs = res.get("isrc", {}).get("recording-list", [])
        return [MBRecording(r, score=1.0) for r in recs]

    def search(
        self, title: str, artist: str | None = None, limit: int = 10
    ) -> list[MBRecording]:
        kwargs: dict = {"recording": title, "limit": limit}
        if artist:
            kwargs["artist"] = artist
        try:
            res = _retry(lambda: musicbrainzngs.search_recordings(**kwargs))
        except musicbrainzngs.ResponseError:
            return []
        return [MBRecording(r) for r in res.get("recording-list", [])]
