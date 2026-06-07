"""Fusion: turn per-source candidate lists into one ranked, diverse playlist.

Each source produces ``(candidate, raw_score)`` edges. We min-max normalise within
the candidate pool per source, weight-sum into a CF score, add a cross-source
agreement (popularity proxy) term, then MMR re-rank for relevance + diversity.

The audio term (Phase 3) slots into ``Candidate.audio_score`` and the fusion below
without changing this interface — when present it's weighted in and the others
renormalised; when absent it simply drops out.
"""

from __future__ import annotations

import math

from rapidfuzz import fuzz

from merlin.config import Settings
from merlin.core.models import Candidate, Track, normalise

# Per-source weights used inside the CF term.
SOURCE_WEIGHTS: dict[str, float] = {
    "listenbrainz": 0.5,
    "lastfm": 0.3,
    "ytm_watch": 0.2,
}


def merge_candidates(
    seed: Track, source_results: dict[str, list[tuple[Track, float]]]
) -> list[Candidate]:
    """Merge per-source results into a deduped candidate pool keyed by mbid/norm."""
    seed_keys = {seed.norm_key}
    if seed.mbid:
        seed_keys.add(seed.mbid)
    if seed.video_id:
        seed_keys.add(seed.video_id)

    pool: dict[str, Candidate] = {}
    for source, results in source_results.items():
        for track, raw in results:
            key = track.mbid or track.norm_key
            if key in seed_keys or track.norm_key in seed_keys:
                continue
            cand = pool.get(key)
            if cand is None:
                cand = Candidate(track=track)
                pool[key] = cand
            else:
                # Enrich the canonical track with anything new we learnt.
                _enrich(cand.track, track)
            # Keep the strongest signal if a source somehow repeats a candidate.
            cand.sources[source] = max(cand.sources.get(source, 0.0), raw)
    return list(pool.values())


def _enrich(target: Track, other: Track) -> None:
    target.mbid = target.mbid or other.mbid
    target.video_id = target.video_id or other.video_id
    target.album = target.album or other.album
    target.duration_ms = target.duration_ms or other.duration_ms
    if not target.artists and other.artists:
        target.artists = other.artists


def score_candidates(candidates: list[Candidate], settings: Settings) -> None:
    """Compute cf/pop/final scores in place."""
    if not candidates:
        return

    active_sources = {s for c in candidates for s in c.sources}
    n_sources = len(active_sources) or 1

    # Per-source max-normalisation across the pool. We divide by the max (not
    # min-max) so the weakest item keeps a non-zero signal — otherwise a candidate
    # present in two sources can be unfairly zeroed in one of them.
    norm: dict[str, dict[int, float]] = {}
    for source in active_sources:
        vals = {i: c.sources[source] for i, c in enumerate(candidates) if source in c.sources}
        hi = max(vals.values())
        norm[source] = {
            i: (v / hi if hi > 1e-9 else 1.0) for i, v in vals.items()
        }

    for i, c in enumerate(candidates):
        c.cf_score = sum(
            SOURCE_WEIGHTS.get(source, 0.1) * norm[source].get(i, 0.0)
            for source in c.sources
        )
        # Cross-source agreement as a popularity proxy (no listen counts available).
        c.pop_score = len(c.sources) / n_sources

    # Renormalise final-score weights over the terms actually present.
    has_audio = any(c.audio_score is not None for c in candidates)
    w_audio = settings.w_audio if has_audio else 0.0
    w_cf, w_pop = settings.w_cf, settings.w_pop
    total = w_audio + w_cf + w_pop or 1.0

    # cf_score is on a weighted-sum scale; normalise it to [0,1] for fair fusion.
    max_cf = max((c.cf_score for c in candidates), default=0.0) or 1.0
    for c in candidates:
        cf = c.cf_score / max_cf
        audio = c.audio_score if c.audio_score is not None else 0.0
        c.final_score = (w_audio * audio + w_cf * cf + w_pop * c.pop_score) / total


def candidate_similarity(a: Candidate, b: Candidate) -> float:
    """Similarity for MMR diversity.

    Uses audio cosine when both sides have it (Phase 3); otherwise a cheap
    artist/title heuristic so we don't stack near-duplicates or one artist.
    """
    if a.audio_score is not None and b.audio_score is not None:
        va, vb = getattr(a, "_vec", None), getattr(b, "_vec", None)
        if va is not None and vb is not None:
            return _cosine(va, vb)
    same_artist = normalise(a.track.primary_artist) == normalise(b.track.primary_artist)
    title_sim = fuzz.token_sort_ratio(
        normalise(a.track.title), normalise(b.track.title)
    ) / 100.0
    base = 0.7 if same_artist and a.track.primary_artist else 0.0
    return min(1.0, base + 0.3 * title_sim)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def mmr_rerank(
    candidates: list[Candidate], size: int, lam: float
) -> list[Candidate]:
    """Maximal Marginal Relevance: balance relevance against intra-list diversity."""
    if not candidates:
        return []
    remaining = sorted(candidates, key=lambda c: c.final_score, reverse=True)
    selected: list[Candidate] = [remaining.pop(0)]
    while remaining and len(selected) < size:
        best_i, best_mmr = 0, -1e9
        for i, c in enumerate(remaining):
            max_sim = max(candidate_similarity(c, s) for s in selected)
            mmr = lam * c.final_score - (1 - lam) * max_sim
            if mmr > best_mmr:
                best_mmr, best_i = mmr, i
        selected.append(remaining.pop(best_i))
    return selected
