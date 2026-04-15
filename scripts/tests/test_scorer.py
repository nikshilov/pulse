import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from extract.scorer import CURRENT_VERSION, score_entity, score_event

def test_version_is_semver_string():
    assert isinstance(CURRENT_VERSION, str)
    assert CURRENT_VERSION.startswith("v")

def test_score_entity_passes_through_model_values():
    raw = {"salience": 0.8, "emotional_weight": 0.9}
    scored = score_entity(raw)
    assert scored["salience_score"] == 0.8
    assert scored["emotional_weight"] == 0.9
    assert scored["scorer_version"] == CURRENT_VERSION

def test_score_entity_clamps_out_of_range():
    raw = {"salience": 1.5, "emotional_weight": -0.2}
    scored = score_entity(raw)
    assert scored["salience_score"] == 1.0
    assert scored["emotional_weight"] == 0.0

def test_score_event_preserves_sentiment_sign():
    raw = {"sentiment": -0.5, "emotional_weight": 0.7}
    scored = score_event(raw)
    assert scored["sentiment"] == -0.5
    assert scored["emotional_weight"] == 0.7
    assert scored["scorer_version"] == CURRENT_VERSION
