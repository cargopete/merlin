"""Phase 2 tests — fusion math, MMR, and the multi-source engine. No network."""

from __future__ import annotations

import pytest

from merlin.config import Settings
from merlin.core.engine import Engine
from merlin.core.fusion import (
    candidate_similarity,
    merge_candidates,
    mmr_rerank,
    score_candidates,
)
from merlin.core.models import Candidate, Track
from merlin.db.database import Database


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path)


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.sqlite")
    yield d
    d.close()


def _t(title, artist, mbid=None, vid=None):
    return Track(title=title, artists=[artist], mbid=mbid, video_id=vid)


# --- merge / score ----------------------------------------------------------


def test_merge_dedups_and_excludes_seed():
    seed = Track(title="Seed", artists=["S"], mbid="seed", video_id="vSeed")
    src = {
        "lastfm": [(_t("A", "X", mbid="m1"), 0.9), (_t("Seed", "S", mbid="seed"), 0.5)],
        "listenbrainz": [(_t("A", "X", mbid="m1"), 100.0)],
    }
    pool = merge_candidates(seed, src)
    keys = {c.track.mbid for c in pool}
    assert keys == {"m1"}  # seed excluded, A deduped across sources
    assert set(pool[0].sources) == {"lastfm", "listenbrainz"}


def test_cross_source_agreement_wins(settings):
    seed = Track(title="Seed", artists=["S"], mbid="seed")
    src = {
        "listenbrainz": [
            (_t("Both", "B", mbid="m1"), 1000.0),
            (_t("Solo", "C", mbid="m2"), 1100.0),
        ],
        "lastfm": [(_t("Both", "B", mbid="m1"), 0.5)],
    }
    pool = merge_candidates(seed, src)
    score_candidates(pool, settings)
    pool.sort(key=lambda c: c.final_score, reverse=True)
    # "Both" wins despite "Solo" having a higher raw LB score, thanks to agreement.
    assert pool[0].track.mbid == "m1"


def test_score_handles_single_candidate(settings):
    seed = Track(title="Seed", artists=["S"], mbid="seed")
    pool = merge_candidates(seed, {"lastfm": [(_t("Only", "O", mbid="m1"), 0.42)]})
    score_candidates(pool, settings)
    assert pool[0].final_score > 0  # no div-by-zero on degenerate pool


# --- similarity / MMR -------------------------------------------------------


def test_candidate_similarity_same_artist_higher():
    a = Candidate(track=_t("Song One", "Artist"))
    b = Candidate(track=_t("Song Two", "Artist"))
    c = Candidate(track=_t("Song One", "Different"))
    assert candidate_similarity(a, b) > candidate_similarity(a, c)


def test_mmr_promotes_diversity():
    # Three high-score same-artist tracks + one lower-score different artist.
    cands = [
        Candidate(track=_t("S1", "A", vid="1")),
        Candidate(track=_t("S2", "A", vid="2")),
        Candidate(track=_t("S3", "A", vid="3")),
        Candidate(track=_t("D1", "B", vid="4")),
    ]
    for c, s in zip(cands, [1.0, 0.95, 0.9, 0.6], strict=False):
        c.final_score = s
    out = mmr_rerank(cands, size=2, lam=0.5)
    artists = {c.track.primary_artist for c in out}
    assert "B" in artists  # diversity pulled the different artist up the order


# --- engine end-to-end with fakes ------------------------------------------


class FakeYTM:
    def get_watch_playlist(self, video_id, radio, limit):
        return [
            _t("Hotel California", "Eagles", vid="vEagles"),
            _t("Dream On", "Aerosmith", vid="vAero"),
        ]


class FakeResolver:
    def mbid_for(self, track):
        track.mbid = track.mbid or "seed-mbid"
        return track.mbid

    def to_ytmusic(self, track):
        if not track.video_id:
            track.video_id = f"v_{track.title}"
        return track


class FakeLabs:
    def similar_recordings(self, mbid, **kw):
        return [
            (_t("Hotel California", "Eagles", mbid="mEagles"), 1169.0),
            (_t("Sultans of Swing", "Dire Straits", mbid="mDire"), 1086.0),
        ]


def test_engine_fusion_dry_run(settings, db):
    eng = Engine(ytm=FakeYTM(), db=db, settings=settings)
    eng._resolver = FakeResolver()
    eng._lb_labs = FakeLabs()
    # Last.fm unavailable (no api key) — source simply skipped.
    seed = Track(title="Bohemian Rhapsody", artists=["Queen"], video_id="vSeed", mbid="seed")
    eng.resolve_seed = lambda q: seed  # bypass YTM search

    result = eng.build_radio("Bohemian Rhapsody", size=5, dry_run=True)
    titles = [t.title for t in result.tracks]
    assert "Hotel California" in titles  # appears in both ytm_watch and LB → top
    assert result.playlist_id is None  # dry run wrote nothing
    assert all(t.video_id for t in result.tracks)
    # cf edges were persisted for MBID-keyed candidates
    rows = db.query("SELECT DISTINCT source FROM cf_edges WHERE seed_mbid='seed'")
    assert {r["source"] for r in rows} >= {"listenbrainz"}
