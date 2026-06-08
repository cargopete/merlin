"""Cross-service identity resolution — the part that dominates perceived quality.

Two hops:
  1. Track -> MusicBrainz recording MBID   (ISRC-first, then scored fuzzy search)
  2. Track -> YouTube Music videoId        (album/song search + fuzzy match)

Both are cached in SQLite so we only pay the network cost once per track. YTM's
catalogue is not deduplicated (official ATV upload vs fan upload vs lyric video vs
remix vs regional variant), so matching is conservative and records a confidence.
"""

from __future__ import annotations

from rapidfuzz import fuzz

from merlin.clients.musicbrainz import MBRecording, MusicBrainzClient
from merlin.clients.ytmusic import YTMusicClient
from merlin.config import Settings, get_settings
from merlin.core.models import Track, normalise
from merlin.db.database import Database, get_db

DURATION_TOL_MS = 5000
TITLE_THRESHOLD = 0.82
ARTIST_THRESHOLD = 0.70


def _sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return fuzz.token_sort_ratio(a, b) / 100.0


def _artist_sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return fuzz.token_set_ratio(a, b) / 100.0


class Resolver:
    def __init__(
        self,
        ytm: YTMusicClient | None = None,
        mb: MusicBrainzClient | None = None,
        db: Database | None = None,
        settings: Settings | None = None,
    ):
        self.settings = settings or get_settings()
        self.ytm = ytm or YTMusicClient(self.settings)
        self.mb = mb or MusicBrainzClient(self.settings)
        self.db = db or get_db(self.settings)

    # --- MBID resolution -----------------------------------------------------

    def mbid_for(self, track: Track) -> str | None:
        if track.mbid:
            return track.mbid

        # Cached via the YTM index?
        if track.video_id:
            cached = self.db.mbid_for_video_id(track.video_id)
            if cached:
                track.mbid = cached
                return cached

        rec = self._best_mb_recording(track)
        if not rec:
            return None

        track.mbid = rec.mbid
        track.isrc = track.isrc or rec.isrc
        self.db.upsert_track(
            rec.mbid,
            isrc=track.isrc,
            title=track.title or rec.title,
            artists=track.artists or rec.artists,
            album=track.album,
            duration_ms=track.duration_ms or rec.duration_ms,
        )
        if track.video_id:
            self.db.cache_yt_mapping(track.video_id, mbid=rec.mbid)
        return rec.mbid

    def _best_mb_recording(self, track: Track) -> MBRecording | None:
        # ISRC-first: one hop, exact.
        if track.isrc:
            recs = self.mb.by_isrc(track.isrc)
            if recs:
                return self._pick(track, recs, isrc_hit=True)

        recs = self.mb.search(track.title, track.primary_artist or None, limit=10)
        if not recs:
            return None
        return self._pick(track, recs, isrc_hit=False)

    def _pick(
        self, track: Track, recs: list[MBRecording], *, isrc_hit: bool
    ) -> MBRecording | None:
        norm_title = normalise(track.title)
        norm_artist = normalise(track.primary_artist)
        best: tuple[float, MBRecording] | None = None
        for rec in recs:
            t = _sim(norm_title, normalise(rec.title))
            a = (
                _artist_sim(norm_artist, normalise(" ".join(rec.artists)))
                if norm_artist
                else 1.0
            )
            dur = self._duration_factor(track.duration_ms, rec.duration_ms)
            # MB's own ext:score nudges ties; ISRC hits start from certainty.
            score = (0.6 * t + 0.3 * a + 0.1 * dur) * (1.0 if isrc_hit else 0.5 + 0.5 * rec.score)
            if isrc_hit and t == 0.0 and norm_title:
                # ISRC can map to a differently-titled recording; still trust it
                score = max(score, 0.85)
            if best is None or score > best[0]:
                best = (score, rec)
        if best and (isrc_hit or best[0] >= 0.6):
            return best[1]
        return None

    @staticmethod
    def _duration_factor(a: int | None, b: int | None) -> float:
        if not a or not b:
            return 0.5
        return 1.0 if abs(a - b) <= DURATION_TOL_MS else 0.0

    # --- YTM videoId resolution ---------------------------------------------

    def to_ytmusic(self, track: Track) -> Track | None:
        if track.video_id:
            return track

        # Cached by MBID?
        if track.mbid:
            cached = self.db.video_id_for_mbid(track.mbid)
            if cached:
                track.video_id = cached
                return track

        query = f"{track.title} {track.primary_artist}".strip()
        if not query:
            return None
        # Read throttling now lives in YTMusicClient (shared ytm_read bucket).
        candidates = self.ytm.search_songs(query, limit=5)

        best = self._best_ytm(track, candidates)
        if best is None:
            return None
        cand, confidence, method = best
        track.video_id = cand.video_id
        self.db.cache_yt_mapping(
            cand.video_id,
            mbid=track.mbid,
            title=cand.title,
            artists=cand.artists,
            album=cand.album,
            duration_ms=cand.duration_ms,
            resolution_method=method,
            confidence=confidence,
        )
        return track

    def _best_ytm(
        self, seed: Track, candidates: list[Track]
    ) -> tuple[Track, float, str] | None:
        norm_title = normalise(seed.title)
        norm_artist = normalise(seed.primary_artist)
        best: tuple[float, Track, str] | None = None
        for cand in candidates:
            if not cand.video_id:
                continue
            t = _sim(norm_title, normalise(cand.title))
            a = (
                _artist_sim(norm_artist, normalise(" ".join(cand.artists)))
                if norm_artist
                else 1.0
            )
            if t < TITLE_THRESHOLD or a < ARTIST_THRESHOLD:
                continue
            dur = self._duration_factor(seed.duration_ms, cand.duration_ms)
            score = 0.55 * t + 0.35 * a + 0.10 * dur
            method = "exact" if (t >= 0.95 and a >= 0.9) else "fuzzy"
            if best is None or score > best[0]:
                best = (score, cand, method)
        if best is None:
            return None
        return best[1], round(best[0], 3), best[2]

    def resolve_many_to_ytmusic(self, tracks: list[Track]) -> list[Track]:
        """Resolve a candidate list to YTM videoIds, dropping the unmatchable."""
        out: list[Track] = []
        for t in tracks:
            if t.video_id:
                out.append(t)
                continue
            resolved = self.to_ytmusic(t)
            if resolved and resolved.video_id:
                out.append(resolved)
        return out
