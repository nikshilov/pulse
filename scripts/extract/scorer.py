"""Salience / emotional_weight / sentiment scoring. Version-pinned for drift handling."""

CURRENT_VERSION = "v1.0"


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def score_entity(raw: dict) -> dict:
    return {
        "salience_score": _clamp(float(raw.get("salience", 0.0))),
        "emotional_weight": _clamp(float(raw.get("emotional_weight", 0.0))),
        "scorer_version": CURRENT_VERSION,
    }


def score_event(raw: dict) -> dict:
    return {
        "sentiment": _clamp(float(raw.get("sentiment", 0.0)), lo=-1.0, hi=1.0),
        "emotional_weight": _clamp(float(raw.get("emotional_weight", 0.0))),
        "scorer_version": CURRENT_VERSION,
    }


def score_fact(raw: dict) -> dict:
    return {
        "confidence": _clamp(float(raw.get("confidence", 1.0))),
        "scorer_version": CURRENT_VERSION,
    }
