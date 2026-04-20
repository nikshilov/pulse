"""Tests for event-level semantic retrieval (retrieval_v2).

Covers:
- happy path: embed events, retrieve by similar query
- idempotent re-embedding (only_missing=True)
- force re-embedding (only_missing=False)
- empty corpus returns []
- missing embeddings table raises
- model mismatch returns []
- recency decay orders results correctly
- top_k respects the returned count
- graceful handling of malformed vector_json
- graceful handling of missing/garbage ts
- unit: _cosine basic properties

Uses the 'fake-local' embedder so tests run offline and deterministically.
Same input text → same vector, so `"тревога сегодня"` will cosine-match the
single event whose text is `"тревога сегодня"` and miss everything else.
"""

from __future__ import annotations

import json
import math
import sqlite3  # noqa: F401 — used by belief check tests
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _fresh_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    migrations_dir = (
        Path(__file__).resolve().parent.parent.parent
        / "internal" / "store" / "migrations"
    )
    for sql_file in sorted(migrations_dir.glob("*.sql")):
        con.executescript(sql_file.read_text())
    return con


def _seed_events(con, *, now: datetime | None = None):
    """Seed 5 events at different ages. Returns list of ids."""
    if now is None:
        now = datetime(2026, 4, 18, tzinfo=timezone.utc)
    specs = [
        # (id, title, description, sentiment, emo, days_ago)
        (1, "engagement",  "Nik predlozhil Ane pozhenitsya v gorah",         2.0, 0.9,   5),
        (2, "panic attack","Panika volnoobrazna, strah smerti, khvatat'",    -2.0, 0.85,  2),
        (3, "pulse bench", "Pulse retrieval beats Mem0 na Nik corpus",        1.5, 0.7,   0),
        (4, "old wound",   "Kristina cheated 6 years behind back",            -2.0, 0.95, 700),
        (5, "mundane",     "Vanya prinyos produkty iz magazina",              0.0, 0.1,  30),
    ]
    for eid, title, desc, sent, emo, days_ago in specs:
        ts = (now - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
        con.execute(
            "INSERT INTO events (id, title, description, sentiment, "
            "emotional_weight, scorer_version, ts) VALUES (?,?,?,?,?,?,?)",
            (eid, title, desc, sent, emo, "test-v0", ts),
        )
    con.commit()
    return [s[0] for s in specs]


# ---------------------------------------------------------------------------
# Embedding (embed_events)
# ---------------------------------------------------------------------------

def test_embed_events_writes_rows_for_all_seeded_events(tmp_path):
    from extract.retrieval_v2 import embed_events
    con = _fresh_db(tmp_path)
    _seed_events(con)

    n = embed_events(con, embedder_model="fake-local")

    assert n == 5
    count = con.execute("SELECT COUNT(*) FROM event_embeddings").fetchone()[0]
    assert count == 5


def test_embed_events_idempotent_when_only_missing(tmp_path):
    from extract.retrieval_v2 import embed_events
    con = _fresh_db(tmp_path)
    _seed_events(con)

    n_first = embed_events(con, embedder_model="fake-local")
    n_second = embed_events(con, embedder_model="fake-local")

    assert n_first == 5
    assert n_second == 0  # all already have embeddings for this model


def test_embed_events_force_rewrites(tmp_path):
    from extract.retrieval_v2 import embed_events
    con = _fresh_db(tmp_path)
    _seed_events(con)
    embed_events(con, embedder_model="fake-local")

    n_force = embed_events(con, embedder_model="fake-local", only_missing=False)

    assert n_force == 5  # all 5 re-written


def test_embed_events_skips_empty_texts(tmp_path):
    from extract.retrieval_v2 import embed_events
    con = _fresh_db(tmp_path)
    # Insert one event with empty description AND empty title fallback.
    con.execute(
        "INSERT INTO events (id, title, description, sentiment, ts) "
        "VALUES (1, '', NULL, 0.0, '2026-04-18T00:00:00Z')"
    )
    con.commit()

    n = embed_events(con, embedder_model="fake-local")

    assert n == 0  # nothing to embed


# ---------------------------------------------------------------------------
# Retrieval (retrieve_events)
# ---------------------------------------------------------------------------

def test_retrieve_events_empty_when_no_embeddings(tmp_path):
    from extract.retrieval_v2 import retrieve_events
    con = _fresh_db(tmp_path)
    _seed_events(con)
    # Deliberately skip embed_events

    out = retrieve_events(con, "Kristina cheated", embedder_model="fake-local")

    assert out == []


def test_retrieve_events_exact_match_wins(tmp_path):
    from extract.retrieval_v2 import embed_events, retrieve_events
    con = _fresh_db(tmp_path)
    _seed_events(con)
    embed_events(con, embedder_model="fake-local")

    # fake-local embeds identical strings to identical vectors → cosine = 1.0.
    # So querying the exact text of event #4 returns event #4 at top.
    out = retrieve_events(
        con, "Kristina cheated 6 years behind back",
        top_k=3, embedder_model="fake-local",
    )

    assert len(out) >= 1
    assert out[0]["id"] == 4
    assert out[0]["cosine"] == pytest.approx(1.0, abs=1e-6)


def test_retrieve_events_top_k_bounds(tmp_path):
    from extract.retrieval_v2 import embed_events, retrieve_events
    con = _fresh_db(tmp_path)
    _seed_events(con)
    embed_events(con, embedder_model="fake-local")

    out = retrieve_events(
        con, "engagement in mountains", top_k=2, embedder_model="fake-local",
    )

    assert len(out) <= 2


def test_retrieve_events_cross_model_yields_empty(tmp_path):
    from extract.retrieval_v2 import embed_events, retrieve_events
    con = _fresh_db(tmp_path)
    _seed_events(con)
    embed_events(con, embedder_model="fake-local")

    # Ask for results keyed to a model we never populated.
    out = retrieve_events(
        con, "anything", embedder_model="openai-text-embedding-3-large",
    )

    assert out == []


def test_retrieve_events_recency_prefers_fresher_when_cosine_equal(tmp_path):
    from extract.retrieval_v2 import embed_events, retrieve_events
    con = _fresh_db(tmp_path)
    now = datetime(2026, 4, 18, tzinfo=timezone.utc)
    # Two events with identical text → identical fake-local vectors →
    # cosine(query, either) is equal. Recency tiebreak picks the newer.
    shared_desc = "nepovtorimoe sobytie kotoroe proishodit dvazhdy"
    con.execute(
        "INSERT INTO events (id, title, description, sentiment, ts) "
        "VALUES (1, 'twin-old', ?, 0.0, ?)",
        (shared_desc, (now - timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
    con.execute(
        "INSERT INTO events (id, title, description, sentiment, ts) "
        "VALUES (2, 'twin-new', ?, 0.0, ?)",
        (shared_desc, (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
    con.commit()
    embed_events(con, embedder_model="fake-local")

    out = retrieve_events(con, shared_desc, top_k=2, embedder_model="fake-local")

    assert [e["id"] for e in out] == [2, 1]  # fresher first


def test_retrieve_events_score_monotonic_with_cosine(tmp_path):
    """Score must be non-increasing in the returned list."""
    from extract.retrieval_v2 import embed_events, retrieve_events
    con = _fresh_db(tmp_path)
    _seed_events(con)
    embed_events(con, embedder_model="fake-local")

    out = retrieve_events(con, "Nik", top_k=5, embedder_model="fake-local")

    scores = [e["score"] for e in out]
    assert scores == sorted(scores, reverse=True)


def test_retrieve_events_skips_malformed_vector_json(tmp_path):
    from extract.retrieval_v2 import embed_events, retrieve_events
    con = _fresh_db(tmp_path)
    _seed_events(con)
    embed_events(con, embedder_model="fake-local")
    # Corrupt event #3's stored vector to garbage.
    con.execute(
        "UPDATE event_embeddings SET vector_json = '{not-json' WHERE event_id = 3"
    )
    con.commit()

    out = retrieve_events(
        con, "Pulse retrieval beats Mem0", top_k=5, embedder_model="fake-local",
    )

    assert all(e["id"] != 3 for e in out)


def test_retrieve_events_handles_missing_ts_gracefully(tmp_path):
    from extract.retrieval_v2 import embed_events, retrieve_events
    con = _fresh_db(tmp_path)
    con.execute(
        "INSERT INTO events (id, title, description, sentiment, ts) "
        "VALUES (1, 't', 'unique text alpha', 0.0, 'not-a-timestamp')"
    )
    con.commit()
    embed_events(con, embedder_model="fake-local")

    # Must not raise despite unparseable ts.
    out = retrieve_events(con, "unique text alpha", embedder_model="fake-local")

    assert len(out) == 1
    assert out[0]["id"] == 1
    # Fallback default days_ago = 30 → recency = exp(-0.001*30) ≈ 0.9704
    assert out[0]["days_ago"] == 30


# ---------------------------------------------------------------------------
# Low-level: _cosine
# ---------------------------------------------------------------------------

def test_cosine_identical_vectors_is_one():
    from extract.retrieval_v2 import _cosine
    v = [1.0, 2.0, -3.0, 0.5]
    assert _cosine(v, v) == pytest.approx(1.0, abs=1e-9)


def test_cosine_orthogonal_vectors_is_zero():
    from extract.retrieval_v2 import _cosine
    assert _cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0, abs=1e-9)


def test_cosine_opposite_vectors_is_negative_one():
    from extract.retrieval_v2 import _cosine
    assert _cosine([1.0, 2.0], [-1.0, -2.0]) == pytest.approx(-1.0, abs=1e-9)


def test_cosine_zero_vector_returns_zero_not_nan():
    from extract.retrieval_v2 import _cosine
    assert _cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_cosine_length_mismatch_returns_zero():
    from extract.retrieval_v2 import _cosine
    assert _cosine([1.0, 2.0], [1.0, 2.0, 3.0]) == 0.0


# ---------------------------------------------------------------------------
# Recency parameter
# ---------------------------------------------------------------------------

def test_lambda_zero_disables_recency(tmp_path):
    """With λ=0, ranking reduces to pure cosine — ties broken by insertion order."""
    from extract.retrieval_v2 import embed_events, retrieve_events
    con = _fresh_db(tmp_path)
    now = datetime(2026, 4, 18, tzinfo=timezone.utc)
    shared = "stable unique text for tie-breaking test"
    con.execute(
        "INSERT INTO events (id, title, description, sentiment, ts) "
        "VALUES (1, 'A', ?, 0.0, ?)",
        (shared, (now - timedelta(days=1000)).strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
    con.execute(
        "INSERT INTO events (id, title, description, sentiment, ts) "
        "VALUES (2, 'B', ?, 0.0, ?)",
        (shared, (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
    con.commit()
    embed_events(con, embedder_model="fake-local")

    out = retrieve_events(
        con, shared, top_k=2, lam=0.0, embedder_model="fake-local",
        use_belief_class=False,
    )

    # With λ=0 AND use_belief_class=False both score the same raw cosine.
    # Post-014 default uses per-belief-class decay; to test caller's explicit
    # uniform λ, opt out of the class-based path.
    assert {e["id"] for e in out} == {1, 2}
    assert out[0]["score"] == pytest.approx(out[1]["score"], abs=1e-9)


# ---------------------------------------------------------------------------
# Belief vocabulary (migration 014)
# ---------------------------------------------------------------------------

def test_axiom_does_not_decay_over_time(tmp_path):
    """An axiom-class event with 1000 days age should score the same cosine×1.

    This is the Nik-core-wound preservation pattern: Kristina (10 years ago)
    must never lose retrieval salience vs a fresh operational event when the
    query matches her semantically.
    """
    from extract.retrieval_v2 import embed_events, retrieve_events
    con = _fresh_db(tmp_path)
    now = datetime(2026, 4, 18, tzinfo=timezone.utc)
    text_a = "Kristina unique marker axiom event text"
    # Axiom event, 1000 days old
    con.execute(
        "INSERT INTO events (id, title, description, sentiment, ts, belief_class) "
        "VALUES (1, 'axiom', ?, 0.0, ?, 'axiom')",
        (text_a, (now - timedelta(days=1000)).strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
    con.commit()
    embed_events(con, embedder_model="fake-local")

    out = retrieve_events(con, text_a, embedder_model="fake-local")

    assert len(out) == 1
    # With belief_class=axiom → λ=0 → recency=1 → score=cos
    assert out[0]["effective_lambda"] == 0.0
    assert out[0]["score"] == pytest.approx(out[0]["cosine"], abs=1e-9)


def test_hypothesis_decays_faster_than_user_model(tmp_path):
    """Two events with same age but different belief_class should rank differently.

    A hypothesis decays 5x faster than user_model. With same query match, fresh
    user_model should outrank fresh hypothesis on equal age.
    """
    from extract.retrieval_v2 import embed_events, retrieve_events
    con = _fresh_db(tmp_path)
    now = datetime(2026, 4, 18, tzinfo=timezone.utc)
    ts_old = (now - timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
    text = "shared text across both belief classes for tie break"
    con.execute(
        "INSERT INTO events (id, title, description, sentiment, ts, belief_class) "
        "VALUES (1, 'user_model', ?, 0.0, ?, 'user_model')",
        (text, ts_old),
    )
    con.execute(
        "INSERT INTO events (id, title, description, sentiment, ts, belief_class) "
        "VALUES (2, 'hypothesis', ?, 0.0, ?, 'hypothesis')",
        (text, ts_old),
    )
    con.commit()
    embed_events(con, embedder_model="fake-local")

    out = retrieve_events(con, text, top_k=2, embedder_model="fake-local")

    ids = [e["id"] for e in out]
    # user_model (λ=0.001) decays less than hypothesis (λ=0.005) → ranks first
    assert ids == [1, 2]


def test_confidence_floor_preserves_salience(tmp_path):
    """An event with confidence_floor > 0 cannot score below cos × floor.

    Tests the axiom-preservation mechanic: Kristina-wound with floor=0.85 stays
    salient even after 10 years of decay.
    """
    from extract.retrieval_v2 import embed_events, retrieve_events
    con = _fresh_db(tmp_path)
    now = datetime(2026, 4, 18, tzinfo=timezone.utc)
    text = "wound floor preservation test unique text"
    # operational event with huge decay (3000 days) + floor=0.85
    con.execute(
        "INSERT INTO events (id, title, description, sentiment, ts, "
        "                    belief_class, confidence_floor) "
        "VALUES (1, 'wound', ?, 0.0, ?, 'operational', 0.85)",
        (text, (now - timedelta(days=3000)).strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
    con.commit()
    embed_events(con, embedder_model="fake-local")

    out = retrieve_events(con, text, embedder_model="fake-local")

    # cos = 1.0 (identical text), floor=0.85, so score >= 0.85
    # Without floor, score = cos × exp(-0.003 × 3000) ≈ cos × 0.00012 ≈ near zero.
    # Floor lifts it back up to 0.85.
    assert out[0]["score"] >= 0.85


def test_retrieve_events_returns_belief_metadata(tmp_path):
    """Returned events include belief_class/confidence_floor/archivable/provenance."""
    from extract.retrieval_v2 import embed_events, retrieve_events
    con = _fresh_db(tmp_path)
    con.execute(
        "INSERT INTO events (id, title, description, sentiment, ts, "
        "belief_class, confidence_floor, archivable, provenance) "
        "VALUES (1, 't', 'unique belief metadata test text', 0.0, "
        "'2026-04-01T00:00:00Z', 'user_model', 0.5, 0, 'manual')"
    )
    con.commit()
    embed_events(con, embedder_model="fake-local")

    out = retrieve_events(con, "unique belief metadata test text",
                          embedder_model="fake-local")

    assert len(out) == 1
    assert out[0]["belief_class"] == "user_model"
    assert out[0]["confidence_floor"] == pytest.approx(0.5)
    assert out[0]["archivable"] == 0
    assert out[0]["provenance"] == "manual"


def test_belief_class_check_constraint_rejects_bad_value(tmp_path):
    """Migration 014 CHECK constraint blocks invalid belief_class values."""
    con = _fresh_db(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO events (id, title, description, sentiment, ts, belief_class) "
            "VALUES (1, 't', 'x', 0, '2026-04-01T00:00:00Z', 'nonsense_class')"
        )


def test_default_belief_class_is_operational(tmp_path):
    """Pre-014 test events that don't set belief_class should default to operational."""
    from extract.retrieval_v2 import embed_events, retrieve_events
    con = _fresh_db(tmp_path)
    con.execute(
        "INSERT INTO events (id, title, description, sentiment, ts) "
        "VALUES (1, 't', 'default class test text', 0.0, '2026-04-01T00:00:00Z')"
    )
    con.commit()
    embed_events(con, embedder_model="fake-local")

    out = retrieve_events(con, "default class test text", embedder_model="fake-local")

    assert out[0]["belief_class"] == "operational"
    # effective lambda = 0.003 (operational)
    assert out[0]["effective_lambda"] == pytest.approx(0.003)
