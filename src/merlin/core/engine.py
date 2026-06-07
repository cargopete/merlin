"""The recommendation engine.

Phase 0 implements a *YTM-native* radio: resolve a seed, pull YouTube Music's own
``get_watch_playlist(radio=True)`` queue, tidy it (dedup, per-artist cap), and
write it back as a playlist. Later phases swap the candidate-generation core for
the multi-source fusion engine behind this same interface.
"""

from __future__ import annotations

from dataclasses import dataclass

from merlin.clients.ytmusic import YTMusicClient
from merlin.config import Settings, get_settings
from merlin.core.models import Track
from merlin.db.database import Database, get_db


@dataclass
class RadioResult:
    seed: Track
    tracks: list[Track]
    playlist_id: str | None
    playlist_url: str | None


class Engine:
    def __init__(
        self,
        ytm: YTMusicClient | None = None,
        db: Database | None = None,
        settings: Settings | None = None,
    ):
        self.settings = settings or get_settings()
        self.ytm = ytm or YTMusicClient(self.settings)
        self.db = db or get_db(self.settings)

    # --- resolution ----------------------------------------------------------

    def resolve_seed(self, query: str) -> Track:
        """Turn a free-text query (or YTM URL) into a seed Track with a videoId."""
        video_id = _extract_video_id(query)
        if video_id:
            row = self.db.query_one(
                "SELECT title, artists, album, duration_ms FROM yt_index WHERE video_id=?",
                (video_id,),
            )
            if row and row["title"]:
                import json

                return Track(
                    title=row["title"],
                    artists=json.loads(row["artists"]) if row["artists"] else [],
                    album=row["album"],
                    duration_ms=row["duration_ms"],
                    video_id=video_id,
                )
            return Track(title=query, video_id=video_id)

        matches = self.ytm.search_songs(query, limit=5)
        if not matches:
            raise ValueError(f"No YouTube Music song found for: {query!r}")
        seed = matches[0]
        self._cache_track(seed, method="search")
        return seed

    def _cache_track(self, track: Track, method: str) -> None:
        if track.video_id:
            self.db.cache_yt_mapping(
                track.video_id,
                title=track.title,
                artists=track.artists,
                album=track.album,
                duration_ms=track.duration_ms,
                resolution_method=method,
                confidence=1.0 if method == "search" else 0.5,
            )

    # --- candidate generation (Phase 0: YTM-native) --------------------------

    def _ytm_watch_candidates(
        self, seed: Track, *, radio: bool, limit: int
    ) -> list[Track]:
        assert seed.video_id
        cands = self.ytm.get_watch_playlist(seed.video_id, radio=radio, limit=limit)
        out: list[Track] = []
        for t in cands:
            if not t.video_id or t.video_id == seed.video_id:
                continue
            self._cache_track(t, method="watch")
            out.append(t)
        return out

    def _tidy(self, candidates: list[Track], size: int) -> list[Track]:
        """Dedup by videoId and apply the per-artist cap."""
        seen: set[str] = set()
        per_artist: dict[str, int] = {}
        out: list[Track] = []
        cap = self.settings.per_artist_cap
        for t in candidates:
            if not t.video_id or t.video_id in seen:
                continue
            key = t.norm_key.split("::", 1)[-1]
            if per_artist.get(key, 0) >= cap:
                continue
            seen.add(t.video_id)
            per_artist[key] = per_artist.get(key, 0) + 1
            out.append(t)
            if len(out) >= size:
                break
        return out

    # --- public ops ----------------------------------------------------------

    def build_radio(
        self, query: str, *, size: int = 50, dry_run: bool = False
    ) -> RadioResult:
        seed = self.resolve_seed(query)
        raw = self._ytm_watch_candidates(seed, radio=True, limit=max(size, 25))
        tracks = self._tidy(raw, size)
        return self._maybe_write(
            seed, tracks, title=f"Radio: {seed.title}", prepend_seed=True, dry_run=dry_run
        )

    def build_similar(
        self, query: str, *, size: int = 50, name: str | None = None, dry_run: bool = False
    ) -> RadioResult:
        seed = self.resolve_seed(query)
        raw = self._ytm_watch_candidates(seed, radio=False, limit=max(size, 25))
        tracks = self._tidy(raw, size)
        title = name or f"Similar to {seed.title}"
        return self._maybe_write(
            seed, tracks, title=title, prepend_seed=False, dry_run=dry_run
        )

    def _maybe_write(
        self,
        seed: Track,
        tracks: list[Track],
        *,
        title: str,
        prepend_seed: bool,
        dry_run: bool,
    ) -> RadioResult:
        if dry_run or not tracks:
            return RadioResult(seed=seed, tracks=tracks, playlist_id=None, playlist_url=None)

        video_ids = [t.video_id for t in tracks if t.video_id]
        if prepend_seed and seed.video_id:
            video_ids = [seed.video_id, *video_ids]

        pid = self.ytm.create_playlist(title, description="Generated by Merlin")
        self.ytm.add_playlist_items(pid, video_ids)
        import json

        self.db.execute(
            "INSERT OR REPLACE INTO playlists_created(playlist_id, seed, params_json) "
            "VALUES(?,?,?)",
            (pid, seed.display(), json.dumps({"title": title, "size": len(video_ids)})),
        )
        return RadioResult(
            seed=seed,
            tracks=tracks,
            playlist_id=pid,
            playlist_url=self.ytm.playlist_url(pid),
        )


def _extract_video_id(query: str) -> str | None:
    import re
    from urllib.parse import parse_qs, urlparse

    query = query.strip()
    if "music.youtube.com" in query or "youtube.com" in query or "youtu.be" in query:
        parsed = urlparse(query)
        if parsed.path.startswith("/watch"):
            vid = parse_qs(parsed.query).get("v", [None])[0]
            if vid:
                return vid
        if "youtu.be" in parsed.netloc:
            return parsed.path.lstrip("/") or None
    # bare 11-char video id
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", query):
        return query
    return None
