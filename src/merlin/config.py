"""Central configuration for Merlin.

Settings load from environment variables (prefix ``MERLIN_``) and an optional
``.env`` file in the working directory. Secrets you would rather not place in env
vars (OAuth tokens, etc.) live in the database / keyring, not here.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# --- Constants ---------------------------------------------------------------

# Audio feature vector layout (Phase 3):
#   1  normalised BPM
#   24 one-hot key_key (12) x key_scale (2)
#   7  high-level probs: happy, aggressive, relaxed, party, acoustic, electronic, danceable
#   3  average_loudness, dynamic_complexity, onset_rate
AUDIO_VECTOR_DIM = 35

# A polite, identifiable User-Agent. MusicBrainz *will* block you without one.
USER_AGENT = "merlin/0.1 ( https://github.com/cargopete/merlin )"


def _default_data_dir() -> Path:
    """XDG-ish data directory, honouring overrides."""
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "merlin"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MERLIN_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Service ---
    host: str = "127.0.0.1"
    port: int = 7654
    # Background APScheduler jobs (library sync + AB cache warm). Daemon only.
    scheduler_enabled: bool = True

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    # --- Storage ---
    data_dir: Path = Field(default_factory=_default_data_dir)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "db.sqlite"

    @property
    def oauth_file(self) -> Path:
        # ytmusicapi OAuth token store
        return self.data_dir / "oauth.json"

    # --- YouTube Music (OAuth, "TVs and Limited Input devices" client) ---
    ytm_client_id: str | None = None
    ytm_client_secret: str | None = None
    ytm_language: str = "en"

    # --- MusicBrainz ---
    # Drop your contact email here so MB can reach you before blocking you.
    contact_email: str = "you@example.com"

    # --- Last.fm ---
    lastfm_api_key: str | None = None
    lastfm_api_secret: str | None = None
    lastfm_user: str | None = None

    # --- ListenBrainz ---
    listenbrainz_token: str | None = None
    listenbrainz_user: str | None = None

    # --- Fusion weights (Phase 2/3) ---
    w_audio: float = 0.4
    w_cf: float = 0.5
    w_pop: float = 0.1
    mmr_lambda: float = 0.7
    per_artist_cap: int = 2

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
