"""Phase 1 tests — JSPF parsing, MBID selection, YTM matching. No network."""

from __future__ import annotations

import pytest

from merlin.clients.listenbrainz import _jspf_to_tracks, _mbid_from_identifier
from merlin.clients.musicbrainz import MBRecording
from merlin.core.models import Track
from merlin.core.resolver import Resolver
from merlin.db.database import Database

MBID_A = "11111111-1111-1111-1111-111111111111"
MBID_B = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.sqlite")
    yield d
    d.close()


def _mb(mbid, title, artist, length=None, score=None):
    rec = {
        "id": mbid,
        "title": title,
        "artist-credit": [{"artist": {"name": artist}}],
    }
    if length:
        rec["length"] = str(length)
    if score is not None:
        rec["ext:score"] = str(score)
    return MBRecording(rec)


class FakeMB:
    def __init__(self, isrc_recs=None, search_recs=None):
        self._isrc = isrc_recs or []
        self._search = search_recs or []
        self.search_called = False

    def by_isrc(self, isrc):
        return list(self._isrc)

    def search(self, title, artist=None, limit=10):
        self.search_called = True
        return list(self._search)


class FakeYTM:
    def __init__(self, results=None, explode=False):
        self._results = results or []
        self.explode = explode
        self.calls = 0

    def search_songs(self, query, limit=5):
        self.calls += 1
        if self.explode:
            raise AssertionError("YTM search should not have been called (cache hit)")
        return list(self._results)


# --- JSPF -------------------------------------------------------------------


def test_mbid_from_identifier_string_and_list():
    url = f"https://musicbrainz.org/recording/{MBID_A}"
    assert _mbid_from_identifier(url) == MBID_A
    assert _mbid_from_identifier([url]) == MBID_A
    assert _mbid_from_identifier(["https://example.com/x", url]) == MBID_A
    assert _mbid_from_identifier(None) is None
    assert _mbid_from_identifier(["nope"]) is None


def test_jspf_to_tracks():
    jspf = {
        "playlist": {
            "track": [
                {
                    "title": "Paranoid Android",
                    "creator": "Radiohead",
                    "identifier": [f"https://musicbrainz.org/recording/{MBID_A}"],
                },
                {"title": "No MBID", "creator": "Someone"},
                {"creator": "skipped — no title"},
            ]
        }
    }
    tracks = _jspf_to_tracks(jspf)
    assert len(tracks) == 2
    assert tracks[0].title == "Paranoid Android"
    assert tracks[0].mbid == MBID_A
    assert tracks[0].primary_artist == "Radiohead"
    assert tracks[1].mbid is None


# --- MBID resolution --------------------------------------------------------


def test_mbid_isrc_first(db):
    mb = FakeMB(isrc_recs=[_mb(MBID_A, "Song", "Artist", length=200000)])
    r = Resolver(ytm=FakeYTM(explode=True), mb=mb, db=db)
    t = Track(title="Song", artists=["Artist"], isrc="US1234567890", duration_ms=200000)
    assert r.mbid_for(t) == MBID_A
    assert not mb.search_called  # ISRC short-circuits the fuzzy search
    # cached into tracks
    row = db.query_one("SELECT isrc FROM tracks WHERE mbid=?", (MBID_A,))
    assert row["isrc"] == "US1234567890"


def test_mbid_fuzzy_search_picks_best(db):
    mb = FakeMB(
        search_recs=[
            _mb(MBID_B, "Totally Different", "Artist", length=200000, score=90),
            _mb(MBID_A, "Bohemian Rhapsody", "Queen", length=355000, score=100),
        ]
    )
    r = Resolver(ytm=FakeYTM(explode=True), mb=mb, db=db)
    t = Track(title="Bohemian Rhapsody", artists=["Queen"], duration_ms=355000)
    assert r.mbid_for(t) == MBID_A


def test_mbid_none_when_no_match(db):
    mb = FakeMB(search_recs=[_mb(MBID_B, "Nothing Alike", "Other Band")])
    r = Resolver(ytm=FakeYTM(explode=True), mb=mb, db=db)
    t = Track(title="Bohemian Rhapsody", artists=["Queen"], duration_ms=355000)
    assert r.mbid_for(t) is None


# --- YTM resolution ---------------------------------------------------------


def test_ytm_match_prefers_duration_tiebreak(db):
    ytm = FakeYTM(
        results=[
            Track(title="Strobe (Live)", artists=["deadmau5"], video_id="vidLive"),
            Track(title="Strobe", artists=["deadmau5"], duration_ms=600000, video_id="vidStudio"),
        ]
    )
    r = Resolver(ytm=ytm, mb=FakeMB(), db=db)
    seed = Track(title="Strobe", artists=["deadmau5"], duration_ms=600000, mbid=MBID_A)
    out = r.to_ytmusic(seed)
    assert out.video_id == "vidStudio"
    assert db.video_id_for_mbid(MBID_A) == "vidStudio"


def test_ytm_rejects_wrong_artist(db):
    ytm = FakeYTM(results=[Track(title="Strobe", artists=["Some Cover Band"], video_id="x")])
    r = Resolver(ytm=ytm, mb=FakeMB(), db=db)
    seed = Track(title="Strobe", artists=["deadmau5"])
    assert r.to_ytmusic(seed) is None


def test_ytm_cache_short_circuits_search(db):
    db.cache_yt_mapping("cachedVid", mbid=MBID_A)
    r = Resolver(ytm=FakeYTM(explode=True), mb=FakeMB(), db=db)
    seed = Track(title="Anything", artists=["X"], mbid=MBID_A)
    out = r.to_ytmusic(seed)
    assert out.video_id == "cachedVid"


def test_resolve_many_drops_unmatchable(db):
    ytm = FakeYTM(results=[Track(title="Strobe", artists=["deadmau5"], video_id="ok")])
    r = Resolver(ytm=ytm, mb=FakeMB(), db=db)
    # First resolves (matches "Strobe"), second won't (title mismatch vs only result)
    tracks = [
        Track(title="Strobe", artists=["deadmau5"]),
        Track(title="Completely Unrelated Thing", artists=["Nobody"]),
    ]
    out = r.resolve_many_to_ytmusic(tracks)
    assert len(out) == 1 and out[0].video_id == "ok"
