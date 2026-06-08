"""ListenBrainz client.

Phase 1 uses the Troi-powered ``lb-radio`` endpoint — the most direct open-
ecosystem analogue to "Start Radio": give it a prompt, get a JSPF playlist of
recording MBIDs back. No auth needed for read.

Prompt syntax (a few of many): ``artist:(Radiohead)``, ``tag:(deep house)``,
``#punk``, ``recs:USERNAME::unlistened``. mode ∈ {easy, medium, hard}.
"""

from __future__ import annotations

import re

import httpx

from merlin.config import USER_AGENT, Settings, get_settings
from merlin.core.models import Track

API_ROOT = "https://api.listenbrainz.org"
LABS_ROOT = "https://labs.api.listenbrainz.org"

# Session-based co-occurrence over (effectively) all listens; 5-min sessions;
# capped per-user contribution; skip the 30 most popular as noise.
# NB: only specific pre-baked algorithm strings are accepted (limit_50 is valid;
# limit_100 returns HTTP 400). Verified against the live endpoint.
DEFAULT_SIMILAR_ALGORITHM = (
    "session_based_days_7500_session_300_contribution_5_threshold_15_limit_50_skip_30"
)


class ListenBrainzAuthError(RuntimeError):
    pass

_MBID_RE = re.compile(
    r"musicbrainz\.org/recording/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})"
)


def _mbid_from_identifier(identifier) -> str | None:
    if isinstance(identifier, str):
        identifier = [identifier]
    for ident in identifier or []:
        m = _MBID_RE.search(ident)
        if m:
            return m.group(1)
    return None


def _jspf_to_tracks(jspf: dict) -> list[Track]:
    tracks: list[Track] = []
    for entry in jspf.get("playlist", {}).get("track", []):
        mbid = _mbid_from_identifier(entry.get("identifier"))
        title = entry.get("title", "")
        creator = entry.get("creator", "")
        if not title:
            continue
        tracks.append(
            Track(
                title=title,
                artists=[creator] if creator else [],
                mbid=mbid,
            )
        )
    return tracks


class ListenBrainzClient:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        headers = {"User-Agent": USER_AGENT}
        if self.settings.listenbrainz_token:
            headers["Authorization"] = f"Token {self.settings.listenbrainz_token}"
        self._client = httpx.Client(
            base_url=API_ROOT, headers=headers, timeout=30, http2=True
        )

    def lb_radio(self, prompt: str, mode: str = "medium") -> list[Track]:
        from merlin.ratelimit import rated_get

        r = rated_get(
            self._client,
            "listenbrainz",
            "/1/explore/lb-radio",
            params={"prompt": prompt, "mode": mode},
        )
        if r.status_code == 401:
            # As of 2025 ListenBrainz requires a token for reads (AI-scraper defence).
            raise ListenBrainzAuthError(
                "ListenBrainz now requires an auth token. Get one free at "
                "https://listenbrainz.org/settings/ and set MERLIN_LISTENBRAINZ_TOKEN."
            )
        r.raise_for_status()
        payload = r.json().get("payload", {})
        jspf = payload.get("jspf", {})
        return _jspf_to_tracks(jspf)

    def close(self) -> None:
        self._client.close()


class ListenBrainzLabsClient:
    """The experimental labs endpoints (separate host, no auth required).

    similar-recordings returns session-based co-occurrence neighbours with inline
    metadata (recording_name, artist_credit_name), so no extra MB hop is needed.
    """

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._client = httpx.Client(
            base_url=LABS_ROOT,
            headers={"User-Agent": USER_AGENT},
            timeout=30,
            http2=True,
        )

    def similar_recordings(
        self, mbid: str, algorithm: str = DEFAULT_SIMILAR_ALGORITHM
    ) -> list[tuple[Track, float]]:
        from merlin.ratelimit import rated_get

        r = rated_get(
            self._client,
            "listenbrainz_labs",
            "/similar-recordings/json",
            params={"recording_mbids": mbid, "algorithm": algorithm},
        )
        r.raise_for_status()
        out: list[tuple[Track, float]] = []
        for item in r.json():
            rec_mbid = item.get("recording_mbid")
            name = item.get("recording_name")
            if not rec_mbid or not name:
                continue
            artist = item.get("artist_credit_name") or ""
            track = Track(
                title=name,
                artists=[artist] if artist else [],
                album=item.get("release_name"),
                mbid=rec_mbid,
            )
            out.append((track, float(item.get("score", 0.0))))
        return out

    def close(self) -> None:
        self._client.close()
