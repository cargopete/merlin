"""Phase 0 smoke tests — DB, models, vectors, service wiring. No network."""

from __future__ import annotations

import pytest

from merlin.config import AUDIO_VECTOR_DIM, Settings
from merlin.core.engine import _extract_video_id
from merlin.core.models import Track, normalise
from merlin.db.database import Database


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.sqlite")
    yield d
    d.close()


def test_normalise():
    assert normalise("Bohemian Rhapsody (Remastered 2011)") == "bohemian rhapsody"
    assert normalise("Café del Mar feat. Someone") == "cafe del mar"
    assert normalise("AC/DC — T.N.T.") == "ac dc t n t"


def test_track_keys():
    t = Track(title="Stairway to Heaven (Live)", artists=["Led Zeppelin", "Guest"])
    assert t.primary_artist == "Led Zeppelin"
    assert t.norm_key == "stairway to heaven::led zeppelin"


@pytest.mark.parametrize(
    "query,expected",
    [
        ("https://music.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("Bohemian Rhapsody", None),
    ],
)
def test_extract_video_id(query, expected):
    assert _extract_video_id(query) == expected


def test_db_schema_and_stats(db):
    stats = db.stats()
    assert "tracks" in stats and stats["tracks"] == 0


def test_token_roundtrip(db):
    db.save_token("ytm", {"access_token": "abc", "n": 1})
    assert db.load_token("ytm") == {"access_token": "abc", "n": 1}


def test_yt_mapping_and_lookup(db):
    db.cache_yt_mapping(
        "vid123",
        mbid="mbid-1",
        title="Song",
        artists=["A"],
        resolution_method="search",
        confidence=1.0,
    )
    assert db.mbid_for_video_id("vid123") == "mbid-1"
    assert db.video_id_for_mbid("mbid-1") == "vid123"
    # COALESCE upsert must not clobber existing mbid with NULL
    db.cache_yt_mapping("vid123", album="Album X")
    assert db.mbid_for_video_id("vid123") == "mbid-1"


def test_cf_edges(db):
    db.add_cf_edge("seed", "cand", "lastfm", 0.8)
    db.add_cf_edge("seed", "cand", "lastfm", 0.9)  # upsert
    rows = db.query("SELECT score FROM cf_edges WHERE seed_mbid='seed'")
    assert len(rows) == 1 and rows[0]["score"] == pytest.approx(0.9)


def test_audio_vector_roundtrip_and_ann(db):
    v1 = [0.1] * AUDIO_VECTOR_DIM
    v2 = [0.9] * AUDIO_VECTOR_DIM
    db.store_audio_vector("m1", v1, "test")
    db.store_audio_vector("m2", v2, "test")
    assert db.get_audio_vector("m1") == pytest.approx(v1)
    near = db.nearest_audio([0.11] * AUDIO_VECTOR_DIM, k=2)
    assert near[0][0] == "m1"  # closest


def test_audio_vector_dim_guard(db):
    with pytest.raises(ValueError):
        db.store_audio_vector("bad", [0.0] * 3, "test")


def test_status_endpoint(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    # Point the singleton db + settings at a temp dir.
    monkeypatch.setenv("MERLIN_DATA_DIR", str(tmp_path))
    import merlin.config as cfg
    import merlin.db.database as dbmod

    cfg.get_settings.cache_clear()
    dbmod._db = None

    from merlin.service.app import app

    with TestClient(app) as client:
        r = client.get("/status")
        assert r.status_code == 200
        body = r.json()
        assert body["service"] == "merlin"
        assert body["auth"]["ytm"] is False  # no oauth.json in temp dir
        assert "tracks" in body["db"]


def test_settings_paths(tmp_path):
    s = Settings(data_dir=tmp_path)
    assert s.db_path == tmp_path / "db.sqlite"
    assert s.oauth_file == tmp_path / "oauth.json"
    assert s.base_url == "http://127.0.0.1:7654"
