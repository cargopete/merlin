"""Phase 4 tests — library sync, AB prefetch + negative cache. No network."""

from __future__ import annotations

import pytest

from merlin.config import Settings
from merlin.core.engine import Engine
from merlin.core.models import Track
from merlin.db.database import Database


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.sqlite")
    yield d
    d.close()


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path)


class FakeYTM:
    def get_library_songs(self, limit=5000):
        return [
            Track(title="Lib1", artists=["A"], album="Al", video_id="v1"),
            Track(title="Lib2", artists=["B"], video_id="v2"),
        ]

    def get_liked_songs(self, limit=5000):
        return [Track(title="Liked", artists=["C"], video_id="v3")]

    def get_history(self):
        return [Track(title="Hist", artists=["D"], video_id="v4")]


def test_sync_library_mirrors_tables(db, settings):
    eng = Engine(ytm=FakeYTM(), db=db, settings=settings)
    counts = eng.sync_library()
    assert counts == {"library": 2, "liked": 1, "history": 1}
    assert db.query_one("SELECT COUNT(*) n FROM library_songs")["n"] == 2
    assert db.query_one("SELECT COUNT(*) n FROM liked_songs")["n"] == 1
    assert db.query_one("SELECT COUNT(*) n FROM history")["n"] == 1
    # library tracks were also cached into yt_index
    assert db.mbid_for_video_id("v1") is None  # no mbid yet, but row exists
    assert db.query_one("SELECT title FROM yt_index WHERE video_id='v1'")["title"] == "Lib1"


def test_sync_history_is_snapshot(db, settings):
    eng = Engine(ytm=FakeYTM(), db=db, settings=settings)
    eng.sync_library()
    eng.sync_library()  # re-run must not accumulate history rows
    assert db.query_one("SELECT COUNT(*) n FROM history")["n"] == 1


class FakeAB:
    def __init__(self, known):
        self.known = known
        self.low_calls = 0

    def low_level_bulk(self, mbids):
        self.low_calls += 1
        return {m: self.known[m][0] for m in mbids if m in self.known}

    def high_level_bulk(self, mbids):
        return {m: self.known[m][1] for m in mbids if m in self.known}


def _ab_data():
    low = {
        "rhythm": {"bpm": 120, "onset_rate": 3.0},
        "tonal": {"key_key": "C", "key_scale": "major"},
        "lowlevel": {"average_loudness": 0.4, "dynamic_complexity": 6.0},
    }
    high = {"highlevel": {"mood_happy": {"all": {"happy": 0.6, "not_happy": 0.4}}}}
    return low, high


def test_prefetch_features_and_negative_cache(db, settings):
    db.cache_yt_mapping("v1", mbid="has-data")
    db.cache_yt_mapping("v2", mbid="no-data")
    eng = Engine(ytm=None, db=db, settings=settings)
    eng._ab = FakeAB({"has-data": _ab_data()})

    stored = eng.prefetch_features(limit=10)
    assert stored == 1
    assert db.get_audio_vector("has-data") is not None
    # missing one is negative-cached: record exists, vector is None
    assert db.get_audio_vector("no-data") is None
    assert db.has_audio_record("no-data") is True

    # Second run must do no work — everything is already recorded.
    eng._ab = FakeAB({"has-data": _ab_data()})
    assert eng.prefetch_features(limit=10) == 0
    assert eng._ab.low_calls == 0


def test_sync_route_requires_auth(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MERLIN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MERLIN_SCHEDULER_ENABLED", "false")
    import merlin.config as cfg
    import merlin.db.database as dbmod

    cfg.get_settings.cache_clear()
    dbmod._db = None

    from merlin.service.app import app

    with TestClient(app) as client:
        r = client.post("/sync", json={})
        assert r.status_code == 401  # no oauth.json → YTMusicError
