"""Tests for retrieval_v3.py (v2_pure backward compat + conditional boosts).

Key properties to verify:
  1. Without user_state → results identical to retrieve_events() v2 (no regression)
  2. With neutral state (max mood < 0.5, HRV ok) → emotion+state boosts OFF
  3. With dominant emotion in mood_vector → emotion boost applied
  4. With body-stressed biometrics → state boost applied
  5. return_chain=True → results reordered via event_chains graph
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from extract.retrieval_v3 import (
    EMOTION_KEYS, UserState, retrieve_events_v3, _keyword_emotion_inference,
    _emo_cosine, _fetch_event_emotion,
)
from extract.retrieval_v2 import retrieve_events


MIGRATIONS_DIR = Path(__file__).parent.parent.parent / "internal" / "store" / "migrations"


@pytest.fixture
def con():
    """In-memory SQLite with migrations 001-015 applied."""
    c = sqlite3.connect(":memory:")
    for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        c.executescript(sql_file.read_text())
    return c


@pytest.fixture
def seed_events(con):
    """Populate a small corpus with events + embeddings + emotions + chains."""
    now = "2026-04-22T00:00:00Z"
    # Simple 4-event corpus: marriage anchor (1), repair loop (2), father wound (22), zasluzhivatel (33)
    events = [
        (1, "marriage anchor", "Nik married to Anya, chronic injury from early comment", -2, 0.8, "2025-03-01T00:00:00Z", "user_model", 0.85, 1, "interactive_memory"),
        (2, "repair loop", "2026-04-09 first working repair loop, 2 min close", 2, 0.6, "2026-04-09T00:00:00Z", "operational", 0.0, 1, "interactive_memory"),
        (22, "father wound", "Father at 5 chained himself, собачка, subservience model", -3, 0.9, "2025-08-01T00:00:00Z", "user_model", 0.9, 1, "interactive_memory"),
        (33, "zasluzhivatel", "Nik named Zasluzhivatel figure 2026-04-17", -2, 0.8, "2026-04-17T00:00:00Z", "user_model", 0.0, 1, "interactive_memory"),
    ]
    try:
        con.executemany(
            "INSERT INTO events (id, title, description, sentiment, emotional_weight, ts, "
            "belief_class, confidence_floor, archivable, provenance) VALUES (?,?,?,?,?,?,?,?,?,?)",
            events,
        )
    except sqlite3.OperationalError:
        # pre-014 schema — fall back
        con.executemany(
            "INSERT INTO events (id, title, description, sentiment, emotional_weight, ts) "
            "VALUES (?,?,?,?,?,?)",
            [(e[0], e[1], e[2], e[3], e[4], e[5]) for e in events],
        )

    # Embeddings (fake 3-dim vectors; use `fake-local` embedder)
    embs = [
        (1, "fake-local", 3, json.dumps([0.9, 0.1, 0.0]), "marriage anchor"),
        (2, "fake-local", 3, json.dumps([0.7, 0.5, 0.1]), "repair loop"),
        (22, "fake-local", 3, json.dumps([0.1, 0.1, 0.9]), "father wound"),
        (33, "fake-local", 3, json.dumps([0.2, 0.2, 0.8]), "zasluzhivatel"),
    ]
    con.executemany(
        "INSERT INTO event_embeddings (event_id, model, dim, vector_json, text_source) "
        "VALUES (?,?,?,?,?)", embs,
    )

    # Emotion tags
    # Event 1: guilt-dominant marriage anchor
    # Event 2: trust/joy repair
    # Event 22: shame-dominant father wound
    # Event 33: shame-dominant zasluzhivatel
    emos = [
        (1, 0.0, 0.4, 0.0, 0.3, 0.1, 0.0, 0.0, 0.0, 0.5, 0.8, "manual", "test", 1.0, now),
        (2, 0.6, 0.2, 0.0, 0.4, 0.8, 0.0, 0.5, 0.3, 0.1, 0.0, "manual", "test", 1.0, now),
        (22, 0.0, 0.8, 0.3, 0.7, 0.0, 0.2, 0.0, 0.0, 0.9, 0.0, "manual", "test", 1.0, now),
        (33, 0.0, 0.6, 0.1, 0.5, 0.2, 0.0, 0.2, 0.1, 0.9, 0.2, "manual", "test", 1.0, now),
    ]
    con.executemany(
        "INSERT INTO event_emotions (event_id, joy, sadness, anger, fear, trust, disgust, "
        "anticipation, surprise, shame, guilt, tagger, tagger_version, confidence, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", emos,
    )

    # Chain edges: 22 → 33 (father wound begets zasluzhivatel)
    con.execute(
        "INSERT INTO event_chains (parent_id, child_id, strength, kind) VALUES (?,?,?,?)",
        (22, 33, 1.0, "causal"),
    )
    con.commit()
    return con


def test_no_state_matches_v2_pure(seed_events):
    """When user_state=None, v3 output == v2 output (deterministic)."""
    con = seed_events
    v2 = retrieve_events(con, "что у меня с Аней?", top_k=3, embedder_model="fake-local")
    v3 = retrieve_events_v3(con, "что у меня с Аней?", top_k=3, embedder_model="fake-local")
    assert len(v2) == len(v3)
    for a, b in zip(v2, v3):
        assert a["id"] == b["id"]
        assert abs(a["score"] - b["score"]) < 1e-6, f"score differs: v2={a['score']} v3={b['score']}"


def test_neutral_state_no_boosts(seed_events):
    """Neutral state (max mood < 0.5, HRV ok) → boosts OFF → same as v2_pure."""
    con = seed_events
    neutral = UserState(
        mood_vector={"joy": 0.3, "trust": 0.3, "anticipation": 0.2},
        sleep_quality=0.7, hrv=70.0, stress_proxy=0.3, hr_trend="stable",
    )
    v2 = retrieve_events(con, "что у меня с Аней?", top_k=3, embedder_model="fake-local")
    v3 = retrieve_events_v3(con, "что у меня с Аней?", top_k=3,
                            embedder_model="fake-local", user_state=neutral)
    for a, b in zip(v2, v3):
        assert a["id"] == b["id"], f"Neutral state should match v2 ordering: {[x['id'] for x in v2]} vs {[x['id'] for x in v3]}"


def test_dominant_emotion_triggers_boost(seed_events):
    """Shame-dominant state boosts shame-aligned events (22, 33)."""
    con = seed_events
    shame_state = UserState(mood_vector={"shame": 0.8, "sadness": 0.4})
    out = retrieve_events_v3(con, "что со мной?", top_k=4,
                             embedder_model="fake-local", user_state=shame_state)
    # Shame-aligned events (22, 33) should appear. At least one should have emotion_boost > 1.0
    boosted = [ev for ev in out if ev.get("emotion_boost", 1.0) > 1.0]
    assert boosted, f"Expected at least one event with emotion_boost > 1.0, got {out}"
    # Events with high shame (22, 33) should be in the boosted set
    boosted_ids = {ev["id"] for ev in boosted}
    assert (22 in boosted_ids or 33 in boosted_ids), f"Expected shame-aligned events (22 or 33) in boosted, got {boosted_ids}"


def test_body_stressed_triggers_state_boost(seed_events):
    """Low sleep + high stress → state boost active."""
    con = seed_events
    stressed = UserState(sleep_quality=0.2, stress_proxy=0.8, hr_trend="elevated_3d")
    out = retrieve_events_v3(con, "я устал", top_k=4,
                             embedder_model="fake-local", user_state=stressed)
    # state_boost key should appear in all results when body is stressed
    for ev in out:
        assert "state_boost" in ev, f"Expected state_boost key in {ev}"
        assert ev["state_boost"] >= 1.0


def test_chain_expansion_reorders(seed_events):
    """return_chain=True reorders results via event_chains graph."""
    con = seed_events
    # Query matches both 22 (father wound) and 33 (zasluzhivatel); chain 22→33 exists
    out_no_chain = retrieve_events_v3(con, "zasluzhivatel shame wound", top_k=3,
                                       embedder_model="fake-local")
    out_chain = retrieve_events_v3(con, "zasluzhivatel shame wound", top_k=3,
                                    embedder_model="fake-local", return_chain=True)
    ids_no = [ev["id"] for ev in out_no_chain]
    ids_chain = [ev["id"] for ev in out_chain]
    # Both 22 and 33 should be present in chain result (connected via edge)
    if 22 in ids_no and 33 in ids_no:
        # Chain expansion should have 22 before 33 (root first)
        assert ids_chain.index(22) < ids_chain.index(33), \
            f"Chain expansion should put 22 (root) before 33 (child), got {ids_chain}"


def test_keyword_emotion_inference():
    """Keyword fallback inference returns expected emotions."""
    d = _keyword_emotion_inference("я очень зол и боюсь")
    assert d["anger"] > 0, d
    assert d["fear"] > 0, d
    d2 = _keyword_emotion_inference("абсолютно нейтральный текст без эмоций")
    assert all(v == 0.0 for v in d2.values()), d2


def test_emotion_cosine_zero_vec():
    """Cosine with zero vector returns 0 (not NaN)."""
    assert _emo_cosine([0.0] * 10, [0.5] * 10) == 0.0
    assert _emo_cosine([0.5] * 10, [0.0] * 10) == 0.0
    # Same vec → cosine 1
    v = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    assert abs(_emo_cosine(v, v) - 1.0) < 1e-6


def test_missing_event_emotions_table_graceful(con):
    """If event_emotions table missing (pre-015 DB), zero vec returned."""
    # con without seed_events = only migrations applied but no data
    con.execute("DROP TABLE IF EXISTS event_emotions")
    con.commit()
    vec = _fetch_event_emotion(con, 1)
    assert vec == [0.0] * 10
