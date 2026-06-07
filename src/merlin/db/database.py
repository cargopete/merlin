"""SQLite access layer with the sqlite-vec extension loaded.

Single-user local tool: one process, WAL mode, a coarse lock around access. Most
of Merlin's latency is network I/O, so synchronous SQLite is plenty. We keep the
surface small and typed-ish; callers deal in dicts and plain values.
"""

from __future__ import annotations

import json
import sqlite3
import struct
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import sqlite_vec

from merlin.config import AUDIO_VECTOR_DIM, Settings, get_settings
from merlin.db.schema import SCHEMA, VEC_SCHEMA


def _pack(vec: Sequence[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.enable_load_extension(True)
        sqlite_vec.load(self._conn)
        self._conn.enable_load_extension(False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.execute("PRAGMA busy_timeout=5000;")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.executescript(VEC_SCHEMA)
            self._conn.commit()

    # --- low-level helpers ---------------------------------------------------

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    # --- tokens --------------------------------------------------------------

    def save_token(self, service: str, data: dict[str, Any]) -> None:
        self.execute(
            "INSERT INTO tokens(service, json, updated_at) VALUES(?,?,datetime('now')) "
            "ON CONFLICT(service) DO UPDATE SET json=excluded.json, updated_at=datetime('now')",
            (service, json.dumps(data)),
        )

    def load_token(self, service: str) -> dict[str, Any] | None:
        row = self.query_one("SELECT json FROM tokens WHERE service=?", (service,))
        return json.loads(row["json"]) if row else None

    # --- tracks --------------------------------------------------------------

    def upsert_track(self, mbid: str, **fields: Any) -> None:
        if "artists" in fields and isinstance(fields["artists"], (list, tuple)):
            fields["artists"] = json.dumps(list(fields["artists"]))
        if "mb_tags_json" in fields and isinstance(fields["mb_tags_json"], (list, tuple)):
            fields["mb_tags_json"] = json.dumps(list(fields["mb_tags_json"]))
        cols = ["mbid", *fields.keys()]
        placeholders = ",".join("?" * len(cols))
        updates = ",".join(f"{c}=excluded.{c}" for c in fields)
        self.execute(
            f"INSERT INTO tracks({','.join(cols)}) VALUES({placeholders}) "
            f"ON CONFLICT(mbid) DO UPDATE SET {updates}, updated_at=datetime('now')",
            (mbid, *fields.values()),
        )

    # --- yt index ------------------------------------------------------------

    def cache_yt_mapping(
        self,
        video_id: str,
        *,
        mbid: str | None = None,
        title: str | None = None,
        artists: list[str] | None = None,
        album: str | None = None,
        duration_ms: int | None = None,
        resolution_method: str | None = None,
        confidence: float | None = None,
    ) -> None:
        self.execute(
            "INSERT INTO yt_index"
            "(video_id, mbid, title, artists, album, duration_ms, resolution_method, confidence,"
            " last_verified) VALUES(?,?,?,?,?,?,?,?,datetime('now')) "
            "ON CONFLICT(video_id) DO UPDATE SET "
            " mbid=COALESCE(excluded.mbid, yt_index.mbid),"
            " title=COALESCE(excluded.title, yt_index.title),"
            " artists=COALESCE(excluded.artists, yt_index.artists),"
            " album=COALESCE(excluded.album, yt_index.album),"
            " duration_ms=COALESCE(excluded.duration_ms, yt_index.duration_ms),"
            " resolution_method=COALESCE(excluded.resolution_method, yt_index.resolution_method),"
            " confidence=COALESCE(excluded.confidence, yt_index.confidence),"
            " last_verified=datetime('now')",
            (
                video_id,
                mbid,
                title,
                json.dumps(artists) if artists is not None else None,
                album,
                duration_ms,
                resolution_method,
                confidence,
            ),
        )

    def video_id_for_mbid(self, mbid: str) -> str | None:
        row = self.query_one(
            "SELECT video_id FROM yt_index WHERE mbid=? ORDER BY confidence DESC LIMIT 1",
            (mbid,),
        )
        return row["video_id"] if row else None

    def mbid_for_video_id(self, video_id: str) -> str | None:
        row = self.query_one("SELECT mbid FROM yt_index WHERE video_id=?", (video_id,))
        return row["mbid"] if row else None

    # --- cf edges ------------------------------------------------------------

    def add_cf_edge(
        self, seed_mbid: str, candidate_mbid: str, source: str, score: float
    ) -> None:
        self.execute(
            "INSERT INTO cf_edges(seed_mbid, candidate_mbid, source, score, fetched_at) "
            "VALUES(?,?,?,?,datetime('now')) "
            "ON CONFLICT(seed_mbid, candidate_mbid, source) DO UPDATE SET "
            " score=excluded.score, fetched_at=datetime('now')",
            (seed_mbid, candidate_mbid, source, score),
        )

    # --- audio vectors -------------------------------------------------------

    def store_audio_vector(self, mbid: str, vector: Sequence[float], source: str) -> None:
        if len(vector) != AUDIO_VECTOR_DIM:
            raise ValueError(
                f"expected {AUDIO_VECTOR_DIM}-dim vector, got {len(vector)}"
            )
        with self._lock:
            self._conn.execute(
                "INSERT INTO audio_features(mbid, feature_vector, source, fetched_at) "
                "VALUES(?,?,?,datetime('now')) "
                "ON CONFLICT(mbid) DO UPDATE SET "
                " feature_vector=excluded.feature_vector, source=excluded.source,"
                " fetched_at=datetime('now')",
                (mbid, _pack(vector), source),
            )
            self._conn.execute("DELETE FROM audio_vec WHERE mbid=?", (mbid,))
            self._conn.execute(
                "INSERT INTO audio_vec(mbid, embedding) VALUES(?,?)",
                (mbid, _pack(vector)),
            )
            self._conn.commit()

    def get_audio_vector(self, mbid: str) -> list[float] | None:
        row = self.query_one(
            "SELECT feature_vector FROM audio_features WHERE mbid=?", (mbid,)
        )
        return _unpack(row["feature_vector"]) if row else None

    def nearest_audio(self, vector: Sequence[float], k: int = 50) -> list[tuple[str, float]]:
        rows = self.query(
            "SELECT mbid, distance FROM audio_vec "
            "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (_pack(vector), k),
        )
        return [(r["mbid"], r["distance"]) for r in rows]

    # --- stats ---------------------------------------------------------------

    def stats(self) -> dict[str, int]:
        tables = [
            "tracks",
            "yt_index",
            "audio_features",
            "cf_edges",
            "library_songs",
            "history",
            "liked_songs",
            "playlists_created",
        ]
        out: dict[str, int] = {}
        for t in tables:
            row = self.query_one(f"SELECT COUNT(*) AS n FROM {t}")
            out[t] = int(row["n"]) if row else 0
        return out

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_db: Database | None = None


def get_db(settings: Settings | None = None) -> Database:
    """Process-wide singleton."""
    global _db
    if _db is None:
        settings = settings or get_settings()
        settings.ensure_dirs()
        _db = Database(settings.db_path)
    return _db
