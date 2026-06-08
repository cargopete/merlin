"""Domain models shared across clients, service and CLI."""

from __future__ import annotations

import re
import unicodedata

from pydantic import BaseModel, Field, PrivateAttr

# Noise we strip from titles before fuzzy comparison.
_PARENS = re.compile(r"\s*[\(\[].*?[\)\]]\s*")
_FEAT = re.compile(r"\s*(feat\.?|ft\.?|featuring)\s.*$", re.IGNORECASE)
_WS = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Lower-case, de-accent, strip parens/feat, collapse whitespace."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = _PARENS.sub(" ", text)
    text = _FEAT.sub(" ", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return _WS.sub(" ", text).strip()


class Track(BaseModel):
    """A resolved (or partially resolved) track travelling through the engine."""

    title: str
    artists: list[str] = Field(default_factory=list)
    album: str | None = None
    duration_ms: int | None = None

    mbid: str | None = None
    isrc: str | None = None
    video_id: str | None = None

    @property
    def primary_artist(self) -> str:
        return self.artists[0] if self.artists else ""

    @property
    def norm_key(self) -> str:
        return f"{normalise(self.title)}::{normalise(self.primary_artist)}"

    def display(self) -> str:
        artists = ", ".join(self.artists) if self.artists else "?"
        return f"{self.title} — {artists}"


class Candidate(BaseModel):
    """A candidate track plus its per-source scores, pre-fusion."""

    track: Track
    sources: dict[str, float] = Field(default_factory=dict)  # source -> raw score
    audio_score: float | None = None
    cf_score: float = 0.0
    pop_score: float = 0.0
    final_score: float = 0.0

    # Audio feature vector, attached during Phase 3 for MMR cosine diversity.
    _vec: list[float] | None = PrivateAttr(default=None)

    @property
    def key(self) -> str:
        return self.track.mbid or self.track.norm_key
