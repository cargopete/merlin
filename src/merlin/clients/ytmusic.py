"""YouTube Music front, via the (unofficial) ytmusicapi.

OAuth-only flow as required since November 2024: you need a Google Cloud OAuth
client of type "TVs and Limited Input devices". Run ``merlin auth ytm`` once to
mint ``oauth.json``; ``RefreshingToken`` then keeps the access token fresh.

Writes are deliberately throttled (~1 op/s) — sigma67's experience is that tight
write loops earn HTTP 400 storms. Be a good citizen.
"""

from __future__ import annotations

from typing import Any

from merlin.config import Settings, get_settings
from merlin.core.models import Track
from merlin.ratelimit import limiter


class YTMusicError(RuntimeError):
    pass


# Headers that must not be replayed from a captured request.
_DROP_HEADERS = {"content-length", "content-encoding"}


def _normalise_header_block(raw: str) -> str:
    """Coerce pasted headers into a ``Name: Value`` block.

    Passes a proper header block straight through. If the paste is the DevTools
    two-column copy (alternating ``name`` / ``value`` lines, with HTTP/2
    pseudo-headers like ``:authority``), it pairs and reformats it.
    """
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    colonish = sum(1 for ln in lines if ": " in ln)
    if colonish >= 4:
        return raw  # already a normal "Name: Value" block

    out: list[str] = []
    i = 0
    while i + 1 < len(lines):
        name, value = lines[i], lines[i + 1]
        i += 2
        if name.startswith(":"):  # HTTP/2 pseudo-header — not a real header
            continue
        if name.lower() in _DROP_HEADERS:
            continue
        out.append(f"{name}: {value}")
    return "\n".join(out)


def _artists_from(entry: dict[str, Any]) -> list[str]:
    arts = entry.get("artists") or []
    return [a["name"] for a in arts if a.get("name")]


def _duration_ms(entry: dict[str, Any]) -> int | None:
    ms = entry.get("duration_seconds")
    if ms:
        return int(ms) * 1000
    return None


def _entry_to_track(entry: dict[str, Any]) -> Track:
    album = entry.get("album")
    album_name = album.get("name") if isinstance(album, dict) else album
    return Track(
        title=entry.get("title", ""),
        artists=_artists_from(entry),
        album=album_name,
        duration_ms=_duration_ms(entry),
        video_id=entry.get("videoId"),
    )


class YTMusicClient:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._yt: Any = None

    # --- auth ----------------------------------------------------------------

    def auth_method(self) -> str | None:
        """Which auth is configured. Browser headers take precedence over OAuth."""
        if self.settings.browser_file.exists():
            return "browser"
        if self.settings.oauth_file.exists():
            return "oauth"
        return None

    def is_authenticated(self) -> bool:
        return self.auth_method() is not None

    def setup_browser(self, headers_raw: str | None = None) -> None:
        """Persist browser-headers auth (browser.json). No Google Cloud needed.

        Accepts either a proper ``Name: Value`` header block or the two-column
        DevTools copy (alternating name/value lines, HTTP/2 pseudo-headers) — the
        latter is normalised so the usual copy-paste mistake just works.
        """
        from ytmusicapi import setup

        if headers_raw is not None:
            headers_raw = _normalise_header_block(headers_raw)
        self.settings.ensure_dirs()
        setup(filepath=str(self.settings.browser_file), headers_raw=headers_raw)

    def setup_oauth(self, open_browser: bool = False) -> None:
        """Run the device OAuth flow and persist oauth.json. Interactive."""
        from ytmusicapi import setup_oauth

        if not (self.settings.ytm_client_id and self.settings.ytm_client_secret):
            raise YTMusicError(
                "MERLIN_YTM_CLIENT_ID and MERLIN_YTM_CLIENT_SECRET must be set "
                "(Google Cloud OAuth client, type 'TVs and Limited Input devices')."
            )
        self.settings.ensure_dirs()
        setup_oauth(
            filepath=str(self.settings.oauth_file),
            client_id=self.settings.ytm_client_id,
            client_secret=self.settings.ytm_client_secret,
            open_browser=open_browser,
        )

    @property
    def yt(self) -> Any:
        if self._yt is None:
            from ytmusicapi import YTMusic

            method = self.auth_method()
            if method == "browser":
                self._yt = YTMusic(
                    str(self.settings.browser_file), language=self.settings.ytm_language
                )
            elif method == "oauth":
                from ytmusicapi import OAuthCredentials

                if not (self.settings.ytm_client_id and self.settings.ytm_client_secret):
                    raise YTMusicError("Missing YTM OAuth client id/secret in settings.")
                self._yt = YTMusic(
                    str(self.settings.oauth_file),
                    oauth_credentials=OAuthCredentials(
                        client_id=self.settings.ytm_client_id,
                        client_secret=self.settings.ytm_client_secret,
                    ),
                    language=self.settings.ytm_language,
                )
            else:
                raise YTMusicError("Not authenticated. Run: merlin auth ytm")
        return self._yt

    # --- reads (≤4/s via shared bucket) --------------------------------------

    def search_songs(self, query: str, limit: int = 5) -> list[Track]:
        limiter("ytm_read").acquire()
        results = self.yt.search(query, filter="songs", limit=limit)
        return [_entry_to_track(r) for r in results if r.get("videoId")]

    def get_song(self, video_id: str) -> dict[str, Any]:
        limiter("ytm_read").acquire()
        return self.yt.get_song(video_id)

    def get_watch_playlist(
        self, video_id: str, *, radio: bool = True, limit: int = 25
    ) -> list[Track]:
        limiter("ytm_read").acquire()
        res = self.yt.get_watch_playlist(videoId=video_id, radio=radio, limit=limit)
        tracks = res.get("tracks", []) if isinstance(res, dict) else []
        return [_entry_to_track(t) for t in tracks if t.get("videoId")]

    def get_library_songs(self, limit: int = 1000) -> list[Track]:
        limiter("ytm_read").acquire()
        return [_entry_to_track(r) for r in self.yt.get_library_songs(limit=limit)]

    def get_liked_songs(self, limit: int = 1000) -> list[Track]:
        limiter("ytm_read").acquire()
        res = self.yt.get_liked_songs(limit=limit)
        return [_entry_to_track(r) for r in res.get("tracks", [])]

    def get_history(self) -> list[Track]:
        limiter("ytm_read").acquire()
        return [_entry_to_track(r) for r in self.yt.get_history()]

    # --- writes (≤1 op / 2s, retried on transient server errors) -------------

    def _write(self, fn):
        from ytmusicapi.exceptions import YTMusicServerError

        from merlin.ratelimit import transient_retry

        limiter("ytm_write").acquire()
        return transient_retry(YTMusicServerError, attempts=4)(fn)()

    def create_playlist(
        self, title: str, description: str = "", privacy: str = "PRIVATE"
    ) -> str:
        pid = self._write(
            lambda: self.yt.create_playlist(title, description, privacy_status=privacy)
        )
        if not isinstance(pid, str):
            raise YTMusicError(f"create_playlist returned unexpected value: {pid!r}")
        return pid

    def add_playlist_items(self, playlist_id: str, video_ids: list[str]) -> Any:
        # add_playlist_items is idempotent across reruns (no duplicate entries).
        return self._write(
            lambda: self.yt.add_playlist_items(playlist_id, video_ids, duplicates=False)
        )

    def playlist_url(self, playlist_id: str) -> str:
        return f"https://music.youtube.com/playlist?list={playlist_id}"
