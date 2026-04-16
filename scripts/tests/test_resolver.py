import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from extract.resolver import resolve_entity, ResolutionDecision

EXISTING = [
    {"id": 10, "canonical_name": "Anna", "kind": "person", "aliases": ["Аня", "Анна"]},
    {"id": 11, "canonical_name": "Fedya", "kind": "person", "aliases": ["Федя"]},
]

def test_exact_alias_match_auto_merge():
    candidate = {"canonical_name": "Аня", "kind": "person", "aliases": []}
    dec = resolve_entity(candidate, EXISTING)
    assert dec.action == "bind_identity"
    assert dec.entity_id == 10
    assert dec.confidence >= 0.98

def test_soft_match_creates_proposal():
    candidate = {"canonical_name": "Анна Петровна", "kind": "person", "aliases": []}
    dec = resolve_entity(candidate, EXISTING)
    # first-name overlap "Анна" ≈ "Anna" → soft match
    assert dec.action in ("proposal", "bind_identity")
    assert 0.7 <= dec.confidence

def test_no_match_creates_open_question():
    candidate = {"canonical_name": "Random Stranger", "kind": "person", "aliases": []}
    dec = resolve_entity(candidate, EXISTING)
    assert dec.action == "new_entity_with_question"
    assert dec.confidence < 0.7
