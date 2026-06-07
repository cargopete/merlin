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
        r = self._client.get(
            "/1/explore/lb-radio", params={"prompt": prompt, "mode": mode}
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
