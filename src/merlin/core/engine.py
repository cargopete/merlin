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
        self._resolver = None
        self._lb = None
        self._lb_labs = None
        self._lastfm = None

    @property
    def resolver(self):
        if self._resolver is None:
            from merlin.core.resolver import Resolver

            self._resolver = Resolver(ytm=self.ytm, db=self.db, settings=self.settings)
        return self._resolver

    @property
    def lb(self):
        if self._lb is None:
            from merlin.clients.listenbrainz import ListenBrainzClient

            self._lb = ListenBrainzClient(self.settings)
        return self._lb

    @property
    def lb_labs(self):
        if self._lb_labs is None:
            from merlin.clients.listenbrainz import ListenBrainzLabsClient

            self._lb_labs = ListenBrainzLabsClient(self.settings)
        return self._lb_labs

    @property
    def lastfm(self):
        if self._lastfm is None:
            from merlin.clients.lastfm import LastFmClient

            self._lastfm = LastFmClient(self.settings)
        return self._lastfm

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

    # --- candidate generation (Phase 2: multi-source) ------------------------

    def _gather(self, seed: Track, *, radio: bool) -> dict[str, list[tuple[Track, float]]]:
        """Fan out to every configured source; return per-source (track, raw)."""
        results: dict[str, list[tuple[Track, float]]] = {}

        # YouTube Music's own watch/radio queue — rank-based scores.
        if seed.video_id:
            watch = self.ytm.get_watch_playlist(seed.video_id, radio=radio, limit=50)
            scored: list[tuple[Track, float]] = []
            n = len(watch)
            for i, t in enumerate(watch):
                if t.video_id and t.video_id != seed.video_id:
                    self._cache_track(t, method="watch")
                    scored.append((t, float(n - i)))
            if scored:
                results["ytm_watch"] = scored

        # Last.fm collaborative similarity (match ∈ [0,1]).
        if self.lastfm.available and seed.title and seed.primary_artist:
            lf = self.lastfm.similar_tracks(seed.primary_artist, seed.title, limit=50)
            if lf:
                results["lastfm"] = lf

        # ListenBrainz session-based co-occurrence (needs the seed MBID).
        if seed.mbid:
            try:
                lb = self.lb_labs.similar_recordings(seed.mbid)
            except Exception:  # labs is experimental — never let it sink the request
                lb = []
            if lb:
                results["listenbrainz"] = lb

        # Persist edges we can key by MBID for later reuse / inspection.
        if seed.mbid:
            for source, lst in results.items():
                for t, raw in lst:
                    if t.mbid:
                        self.db.add_cf_edge(seed.mbid, t.mbid, source, raw)
        return results

    def _fusion_build(
        self,
        seed: Track,
        *,
        size: int,
        radio: bool,
        prepend_seed: bool,
        title: str,
        dry_run: bool,
    ) -> RadioResult:
        from merlin.core.fusion import merge_candidates, mmr_rerank, score_candidates

        source_results = self._gather(seed, radio=radio)
        candidates = merge_candidates(seed, source_results)
        if not candidates:
            return RadioResult(seed=seed, tracks=[], playlist_id=None, playlist_url=None)

        score_candidates(candidates, self.settings)
        candidates.sort(key=lambda c: c.final_score, reverse=True)

        # Resolve the strongest candidates to YTM videoIds (cached). Cap the work.
        resolved: list = []
        for cand in candidates:
            if len(resolved) >= size * 2:
                break
            rt = self.resolver.to_ytmusic(cand.track)
            if rt and rt.video_id:
                resolved.append(cand)

        ranked = mmr_rerank(resolved, size, self.settings.mmr_lambda)
        tracks = self._tidy([c.track for c in ranked], size)
        return self._maybe_write(
            seed, tracks, title=title, prepend_seed=prepend_seed, dry_run=dry_run
        )

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
        self.resolver.mbid_for(seed)  # best-effort, enables the LB source
        return self._fusion_build(
            seed,
            size=size,
            radio=True,
            prepend_seed=True,
            title=f"Radio: {seed.title}",
            dry_run=dry_run,
        )

    def build_similar(
        self, query: str, *, size: int = 50, name: str | None = None, dry_run: bool = False
    ) -> RadioResult:
        seed = self.resolve_seed(query)
        self.resolver.mbid_for(seed)
        return self._fusion_build(
            seed,
            size=size,
            radio=False,
            prepend_seed=False,
            title=name or f"Similar to {seed.title}",
            dry_run=dry_run,
        )

    def build_lb_radio(
        self,
        prompt: str,
        *,
        mode: str = "medium",
        size: int = 50,
        name: str | None = None,
        dry_run: bool = False,
    ) -> RadioResult:
        """ListenBrainz lb-radio passthrough: prompt -> JSPF -> YTM playlist."""
        lb_tracks = self.lb.lb_radio(prompt, mode=mode)
        if not lb_tracks:
            raise ValueError(f"lb-radio returned nothing for prompt: {prompt!r}")
        resolved = self.resolver.resolve_many_to_ytmusic(lb_tracks[: size * 2])
        tracks = self._tidy(resolved, size)
        seed = Track(title=prompt)
        title = name or f"LB Radio: {prompt}"
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
