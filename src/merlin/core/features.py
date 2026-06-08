"""AcousticBrainz feature → fixed-dimension vector (Phase 3).

35-dim layout (see AUDIO_VECTOR_DIM):
  [0]      normalised BPM            (60–200 BPM → [0,1])
  [1:25]   one-hot key_key×key_scale (12 keys × {major,minor} = 24)
  [25:32]  high-level positive-class probs: happy, aggressive, relaxed, party,
           acoustic, electronic, danceable
  [32]     average_loudness
  [33]     dynamic_complexity        (/20, clipped)
  [34]     onset_rate                (/10, clipped)

Every dim is squeezed into [0,1] so cosine similarity treats them comparably.
Partial coverage is normal — missing blocks are left as zeros; an all-zero vector
means "no usable AB data" and is not stored.
"""

from __future__ import annotations

from merlin.config import AUDIO_VECTOR_DIM

KEYS = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
SCALES = ["major", "minor"]

# High-level model name -> positive-class key inside its "all" dict.
HIGH_LEVEL_MODELS = [
    ("mood_happy", "happy"),
    ("mood_aggressive", "aggressive"),
    ("mood_relaxed", "relaxed"),
    ("mood_party", "party"),
    ("mood_acoustic", "acoustic"),
    ("mood_electronic", "electronic"),
    ("danceability", "danceable"),
]


def _clip01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def build_vector(low: dict | None, high: dict | None) -> list[float] | None:
    """Build the 35-dim feature vector. Returns None if there's no usable data."""
    vec = [0.0] * AUDIO_VECTOR_DIM

    if low:
        rhythm = low.get("rhythm", {})
        tonal = low.get("tonal", {})
        lowlevel = low.get("lowlevel", {})

        bpm = rhythm.get("bpm")
        if bpm:
            vec[0] = _clip01((float(bpm) - 60.0) / 140.0)

        key, scale = tonal.get("key_key"), tonal.get("key_scale")
        if key in KEYS and scale in SCALES:
            vec[1 + KEYS.index(key) * 2 + SCALES.index(scale)] = 1.0

        vec[32] = _clip01(float(lowlevel.get("average_loudness", 0.0)))
        vec[33] = _clip01(float(lowlevel.get("dynamic_complexity", 0.0)) / 20.0)
        vec[34] = _clip01(float(rhythm.get("onset_rate", 0.0)) / 10.0)

    if high:
        hl = high.get("highlevel", {})
        for idx, (model, positive) in enumerate(HIGH_LEVEL_MODELS):
            entry = hl.get(model)
            if entry and "all" in entry:
                vec[25 + idx] = _clip01(float(entry["all"].get(positive, 0.0)))

    return vec if any(vec) else None
