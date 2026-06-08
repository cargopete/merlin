"""Last.fm client — collaborative similarity from scrobble co-occurrence.

track.getSimilar returns ranked similar tracks with a ``match`` in [0, 1]. Needs
only an api_key. Rate-limited via the shared token bucket and retried on Last.fm's
"29 — Rate Limit Exceeded" / temporary errors. We deliberately avoid per-item
``get_mbid()`` calls (each is a hidden getInfo request — a 50-item storm); mbids
are advisory anyway and the resolver re-derives them through MusicBrainz.
"""

from __future__ import annotations

import time

from merlin.config import Settings, get_settings
from merlin.core.models import Track

# Last.fm error codes worth retrying: service offline, temporary, rate limit.
_RETRYABLE_STATUS = {"11", "16", "29"}


class LastFmClient:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._net = None

    @property
    def available(self) -> bool:
        return bool(self.settings.lastfm_api_key)

    @property
    def net(self):
        if self._net is None:
            import pylast

            if not self.settings.lastfm_api_key:
                raise RuntimeError("MERLIN_LASTFM_API_KEY is not set.")
            self._net = pylast.LastFMNetwork(
                api_key=self.settings.lastfm_api_key,
                api_secret=self.settings.lastfm_api_secret or "",
            )
        return self._net

    def _call(self, fn, attempts: int = 4):
        """Rate-limited call with retry on transient Last.fm errors."""
        import pylast

        from merlin.ratelimit import limiter

        bucket = limiter("lastfm")
        last_exc: Exception | None = None
        for i in range(attempts):
            bucket.acquire()
            try:
                return fn()
            except pylast.WSError as e:
                last_exc = e
                if str(getattr(e, "status", "")) in _RETRYABLE_STATUS:
                    time.sleep(min(2**i, 20))
                    continue
                raise
        if last_exc:
            raise last_exc
        return None

    def similar_tracks(
        self, artist: str, title: str, limit: int = 50
    ) -> list[tuple[Track, float]]:
        import pylast

        try:
            similar = self._call(
                lambda: self.net.get_track(artist, title).get_similar(limit=limit)
            )
        except pylast.WSError:
            return []
        out: list[tuple[Track, float]] = []
        for entry in similar or []:
            item = entry.item
            out.append(
                (
                    Track(
                        title=item.get_name(),
                        artists=[item.get_artist().get_name()],
                    ),
                    float(entry.match) if entry.match is not None else 0.0,
                )
            )
        return out

    def similar_artists(self, artist: str, limit: int = 30) -> list[tuple[str, float]]:
        import pylast

        try:
            similar = self._call(
                lambda: self.net.get_artist(artist).get_similar(limit=limit)
            )
        except pylast.WSError:
            return []
        return [
            (entry.item.get_name(), float(entry.match) if entry.match else 0.0)
            for entry in similar or []
        ]
