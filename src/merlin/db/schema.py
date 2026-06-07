"""Database schema. One SQLite file holds the lot.

The design centres on the MusicBrainz recording MBID as the join key between
every external service. Where we don't (yet) have an MBID we still key rows by a
normalised ``(title, artist)`` so resolution can be filled in lazily.
"""

from __future__ import annotations

from merlin.config import AUDIO_VECTOR_DIM

# Ordinary tables ------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Encrypted-at-rest preferred (keyring); this is the on-disk fallback.
CREATE TABLE IF NOT EXISTS tokens (
    service    TEXT PRIMARY KEY,
    json       TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Canonical track records, keyed by MusicBrainz recording MBID.
CREATE TABLE IF NOT EXISTS tracks (
    mbid         TEXT PRIMARY KEY,
    isrc         TEXT,
    title        TEXT,
    artists      TEXT,           -- JSON array of names
    album        TEXT,
    duration_ms  INTEGER,
    mb_tags_json TEXT,           -- JSON array of genre tags
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tracks_isrc ON tracks(isrc);

-- YouTube Music resolution cache. video_id <-> mbid (the dominant error source).
CREATE TABLE IF NOT EXISTS yt_index (
    video_id          TEXT PRIMARY KEY,
    mbid              TEXT,
    title             TEXT,
    artists           TEXT,       -- JSON array
    album             TEXT,
    duration_ms       INTEGER,
    resolution_method TEXT,       -- isrc | exact | fuzzy | search | manual
    confidence        REAL,
    last_verified     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_yt_mbid ON yt_index(mbid);

-- Optional Spotify provenance (legacy ISRC source only).
CREATE TABLE IF NOT EXISTS spotify_index (
    spotify_id TEXT PRIMARY KEY,
    mbid       TEXT
);

-- Audio feature vectors, mirrored into the vec0 virtual table below.
CREATE TABLE IF NOT EXISTS audio_features (
    mbid           TEXT PRIMARY KEY,
    feature_vector BLOB,          -- packed float32, AUDIO_VECTOR_DIM dims
    source         TEXT,          -- acousticbrainz | essentia | tags
    fetched_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Collaborative-filtering / co-occurrence edges from any source.
CREATE TABLE IF NOT EXISTS cf_edges (
    seed_mbid      TEXT NOT NULL,
    candidate_mbid TEXT NOT NULL,
    source         TEXT NOT NULL, -- listenbrainz | lastfm | ytm_watch
    score          REAL NOT NULL,
    fetched_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (seed_mbid, candidate_mbid, source)
);
CREATE INDEX IF NOT EXISTS idx_cf_seed ON cf_edges(seed_mbid);

-- Local mirror of the user's YTM library / history / likes.
CREATE TABLE IF NOT EXISTS library_songs (
    video_id   TEXT PRIMARY KEY,
    title      TEXT,
    artists    TEXT,
    album      TEXT,
    synced_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS history (
    video_id   TEXT,
    title      TEXT,
    artists    TEXT,
    played_at  TEXT,
    synced_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS liked_songs (
    video_id   TEXT PRIMARY KEY,
    title      TEXT,
    artists    TEXT,
    synced_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Idempotent playlist re-creation bookkeeping.
CREATE TABLE IF NOT EXISTS playlists_created (
    playlist_id TEXT PRIMARY KEY,
    seed        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    params_json TEXT
);
"""

# vec0 virtual table is created separately (needs the extension loaded).
VEC_SCHEMA = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS audio_vec USING vec0(
    mbid TEXT PRIMARY KEY,
    embedding float[{AUDIO_VECTOR_DIM}]
);
"""
