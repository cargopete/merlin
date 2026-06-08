"""Phase 3 tests — AB vector building and audio application. No network."""

from __future__ import annotations

import pytest

from merlin.config import AUDIO_VECTOR_DIM, Settings
from merlin.core.engine import Engine
from merlin.core.features import KEYS, SCALES, build_vector
from merlin.core.models import Candidate, Track
from merlin.db.database import Database


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.sqlite")
    yield d
    d.close()


def _low(bpm=130, key="A", scale="minor", loud=0.5, dyn=10.0, onset=5.0):
    return {
        "rhythm": {"bpm": bpm, "onset_rate": onset},
        "tonal": {"key_key": key, "key_scale": scale},
        "lowlevel": {"average_loudness": loud, "dynamic_complexity": dyn},
    }


def _high(happy=0.8, danceable=0.7):
    return {
        "highlevel": {
            "mood_happy": {"all": {"happy": happy, "not_happy": 1 - happy}},
            "danceability": {"all": {"danceable": danceable, "not_danceable": 1 - danceable}},
        }
    }


def test_build_vector_layout():
    v = build_vector(_low(), _high())
    assert v is not None and len(v) == AUDIO_VECTOR_DIM
    assert v[0] == pytest.approx((130 - 60) / 140)  # bpm
    key_idx = 1 + KEYS.index("A") * 2 + SCALES.index("minor")
    assert v[key_idx] == 1.0
    assert sum(v[1:25]) == 1.0  # exactly one key bit set
    assert v[25] == pytest.approx(0.8)  # mood_happy positive class
    assert v[31] == pytest.approx(0.7)  # danceability positive class
    assert v[32] == pytest.approx(0.5)  # average_loudness
    assert v[33] == pytest.approx(0.5)  # dynamic_complexity / 20
    assert v[34] == pytest.approx(0.5)  # onset_rate / 10


def test_build_vector_partial_and_empty():
    only_low = build_vector(_low(), None)
    assert only_low is not None and sum(only_low[25:32]) == 0.0  # no mood block
    only_high = build_vector(None, _high())
    assert only_high is not None and only_high[0] == 0.0  # no bpm
    assert build_vector(None, None) is None
    assert build_vector({}, {}) is None


def test_build_vector_clips_extreme_bpm():
    assert build_vector(_low(bpm=400), None)[0] == 1.0
    assert build_vector(_low(bpm=10), None)[0] == 0.0


class FakeAB:
    """Returns AB data only for mbids it 'knows' about."""

    def __init__(self, known: dict[str, tuple[dict, dict]]):
        self.known = known
        self.low_calls = 0

    def low_level_bulk(self, mbids):
        self.low_calls += 1
        return {m: self.known[m][0] for m in mbids if m in self.known}

    def high_level_bulk(self, mbids):
        return {m: self.known[m][1] for m in mbids if m in self.known}


def test_apply_audio_sets_cosine_and_caches(db):
    eng = Engine(ytm=None, db=db, settings=Settings(data_dir=db.path.parent))
    eng._ab = FakeAB(
        {
            "seed": (_low(bpm=130, key="A", scale="minor"), _high(0.8, 0.7)),
            "twin": (_low(bpm=130, key="A", scale="minor"), _high(0.8, 0.7)),
            "other": (_low(bpm=70, key="C", scale="major"), _high(0.1, 0.1)),
        }
    )
    seed = Track(title="Seed", mbid="seed")
    cands = [
        Candidate(track=Track(title="Twin", mbid="twin")),
        Candidate(track=Track(title="Other", mbid="other")),
        Candidate(track=Track(title="NoData", mbid="missing")),
    ]
    eng._apply_audio(seed, cands)

    twin, other, nodata = cands
    assert twin.audio_score == pytest.approx(1.0, abs=1e-6)  # identical features
    assert other.audio_score is not None and other.audio_score < twin.audio_score
    assert nodata.audio_score is None  # AB had nothing → term stays out
    assert twin._vec is not None
    # vectors were persisted
    assert db.get_audio_vector("seed") is not None


def test_apply_audio_noop_without_seed_mbid(db):
    eng = Engine(ytm=None, db=db, settings=Settings(data_dir=db.path.parent))
    eng._ab = FakeAB({})
    cands = [Candidate(track=Track(title="X", mbid="x"))]
    eng._apply_audio(Track(title="Seed"), cands)  # seed has no mbid
    assert cands[0].audio_score is None
