"""Last.fm client — collaborative similarity from scrobble co-occurrence.

track.getSimilar returns ranked similar tracks with a ``match`` in [0, 1]. Needs
only an api_key. Keep well under a few requests/second and cache aggressively;
Last.fm will suspend chatty apps.
"""

from __future__ import annotations

from merlin.config import Settings, get_settings
from merlin.core.models import Track


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

    def similar_tracks(
        self, artist: str, title: str, limit: int = 50
    ) -> list[tuple[Track, float]]:
        import pylast

        try:
            track = self.net.get_track(artist, title)
            similar = track.get_similar(limit=limit)
        except pylast.WSError:
            return []
        out: list[tuple[Track, float]] = []
        for entry in similar:
            item = entry.item
            mbid = None
            try:
                mbid = item.get_mbid() or None  # advisory; re-resolve if doubtful
            except pylast.WSError:
                mbid = None
            out.append(
                (
                    Track(
                        title=item.get_name(),
                        artists=[item.get_artist().get_name()],
                        mbid=mbid,
                    ),
                    float(entry.match) if entry.match is not None else 0.0,
                )
            )
        return out

    def similar_artists(self, artist: str, limit: int = 30) -> list[tuple[str, float]]:
        import pylast

        try:
            similar = self.net.get_artist(artist).get_similar(limit=limit)
        except pylast.WSError:
            return []
        return [
            (entry.item.get_name(), float(entry.match) if entry.match else 0.0)
            for entry in similar
        ]
