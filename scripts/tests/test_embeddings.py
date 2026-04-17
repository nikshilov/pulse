"""Tests for the Phase-3 embeddings POC.

Covers:
  - `extract.embedder.embed_texts` with the `fake-local` backend
  - `pulse_consolidate.embed_entities` UPSERT behaviour
  - `extract.retrieval.retrieve_context` with semantic=on (hybrid path)
  - `run_consolidation(embed_model=...)` plumbing

Everything here uses the deterministic hash-based `fake-local` backend — no
API calls, no OpenAI dependency, fully reproducible on any dev machine.
"""

import json
import sqlite3
from pathlib import Path

import pytest


def _fresh_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    migrations_dir = (
        Path(__file__).resolve().parent.parent.parent
        / "internal"
        / "store"
        / "migrations"
    )
    for sql_file in sorted(migrations_dir.glob("*.sql")):
        con.executescript(sql_file.read_text())
    return con, db_path


# ---------------------------------------------------------------------------
# fake-local embedder properties
# ---------------------------------------------------------------------------

def test_embed_texts_fake_local_deterministic():
    from extract.embedder import embed_texts

    v1 = embed_texts(["тревожно вечером"], model="fake-local")[0]
    v2 = embed_texts(["тревожно вечером"], model="fake-local")[0]
    assert v1 == v2, "fake-local must be bit-for-bit deterministic across calls"


def test_embed_texts_fake_local_different_inputs_different_vectors():
    from extract.embedder import embed_texts

    vectors = embed_texts(
        ["anxiety", "loneliness", "garden", "pulse"], model="fake-local"
    )
    # All four vectors should be distinct (collision probability ~0 for SHA-256)
    as_tuples = {tuple(v) for v in vectors}
    assert len(as_tuples) == 4


def test_embed_texts_fake_local_correct_dim():
    from extract.embedder import embed_texts

    vec = embed_texts(["hello"], model="fake-local")[0]
    assert len(vec) == 128
    # Every element should be a finite float in [-1, 1]
    for x in vec:
        assert isinstance(x, float)
        assert -1.0 <= x <= 1.0


def test_embed_texts_unknown_model_raises():
    from extract.embedder import embed_texts

    with pytest.raises(ValueError, match="Unknown embedding model"):
        embed_texts(["x"], model="not-a-model")


# ---------------------------------------------------------------------------
# embed_entities UPSERT
# ---------------------------------------------------------------------------

def _seed_three_entities(con):
    now = "2026-04-16T00:00:00Z"
    con.execute(
        "INSERT INTO entities (id, canonical_name, kind, aliases, first_seen, last_seen, salience_score) "
        "VALUES (1, 'Anna', 'person', ?, ?, ?, 0.9)",
        (json.dumps(["Аня"]), now, now),
    )
    con.execute(
        "INSERT INTO entities (id, canonical_name, kind, aliases, first_seen, last_seen, salience_score) "
        "VALUES (2, 'Pulse', 'project', ?, ?, ?, 0.8)",
        (json.dumps(["pulse-engine"]), now, now),
    )
    con.execute(
        "INSERT INTO entities (id, canonical_name, kind, aliases, first_seen, last_seen, salience_score) "
        "VALUES (3, 'loneliness', 'concept', ?, ?, ?, 0.3)",
        (json.dumps(["одиночество", "мне плохо"]), now, now),
    )


def test_embed_entities_upserts_row_per_entity(tmp_path):
    from pulse_consolidate import embed_entities

    con, _ = _fresh_db(tmp_path)
    _seed_three_entities(con)

    n = embed_entities(con, embedder_model="fake-local")
    assert n == 3

    rows = con.execute(
        "SELECT entity_id, model, dim, vector_json FROM entity_embeddings ORDER BY entity_id"
    ).fetchall()
    assert [r[0] for r in rows] == [1, 2, 3]
    for _, model, dim, vector_json in rows:
        assert model == "fake-local"
        assert dim == 128
        vec = json.loads(vector_json)
        assert len(vec) == 128


def test_embed_entities_skips_existing_when_only_missing_true(tmp_path):
    from pulse_consolidate import embed_entities

    con, _ = _fresh_db(tmp_path)
    _seed_three_entities(con)

    # First pass embeds all 3.
    assert embed_entities(con, embedder_model="fake-local", only_missing=True) == 3
    # Second pass: all present, last_seen <= updated_at → zero new rows.
    assert embed_entities(con, embedder_model="fake-local", only_missing=True) == 0


def test_embed_entities_text_source_includes_aliases_and_kind(tmp_path):
    """text_source must include canonical_name, kind, aliases — the retrieval
    substrate we want the real embedder to leverage later."""
    from pulse_consolidate import embed_entities

    con, _ = _fresh_db(tmp_path)
    _seed_three_entities(con)
    embed_entities(con, embedder_model="fake-local")

    row = con.execute(
        "SELECT text_source FROM entity_embeddings WHERE entity_id = 3"
    ).fetchone()
    text = row[0]
    assert "loneliness" in text
    assert "kind=concept" in text
    assert "одиночество" in text
    assert "мне плохо" in text


# ---------------------------------------------------------------------------
# retrieve_context semantic flag
# ---------------------------------------------------------------------------

def test_retrieve_context_semantic_off_is_default_and_keyword_only(tmp_path):
    """Default (semantic=False) must NOT add `semantic_seeds` and must keep
    retrieval_method='keyword'. Zero-regression contract for the 147 existing
    tests."""
    from extract.retrieval import retrieve_context

    con, _ = _fresh_db(tmp_path)
    _seed_three_entities(con)
    result = retrieve_context(con, "Anna")
    assert result["retrieval_method"] == "keyword"
    assert "semantic_seeds" not in result


def test_retrieve_context_semantic_on_marks_method_hybrid(tmp_path):
    from extract.retrieval import retrieve_context
    from pulse_consolidate import embed_entities

    con, _ = _fresh_db(tmp_path)
    _seed_three_entities(con)
    embed_entities(con, embedder_model="fake-local")

    result = retrieve_context(con, "Anna", semantic=True)
    assert result["retrieval_method"] == "hybrid"
    assert "semantic_seeds" in result


def test_retrieve_context_semantic_on_unions_seeds(tmp_path, monkeypatch):
    """semantic=True must inject cosine-top-K ids into the seed set even when
    keyword misses them.

    fake-local is not semantically meaningful, so to test the UNION logic
    deterministically we inject a fake `embed_texts` that returns a vector
    identical to the stored vector of a target entity. Cosine = 1.0 → that
    entity dominates the semantic top-N → must appear in matched_entities
    despite the query message containing no token in any alias list.
    """
    from extract.retrieval import retrieve_context
    from pulse_consolidate import embed_entities

    con, _ = _fresh_db(tmp_path)
    _seed_three_entities(con)
    embed_entities(con, embedder_model="fake-local")

    # Target: entity id=3 (loneliness). We want to force it into the seed set
    # via the semantic channel for a query ("XYZNOMATCH") that keyword retrieval
    # cannot possibly match.
    target_row = con.execute(
        "SELECT vector_json FROM entity_embeddings WHERE entity_id = 3"
    ).fetchone()
    target_vec = json.loads(target_row[0])

    # Monkeypatch embed_texts ONLY inside extract.embedder so the query vector
    # aligns perfectly with the loneliness entity vector.
    import extract.embedder as embedder_mod

    def _fake_embed(texts, model="fake-local"):
        # Same structural contract (list-of-vectors), but the query vector
        # is pinned to target_vec.
        return [target_vec for _ in texts]

    monkeypatch.setattr(embedder_mod, "embed_texts", _fake_embed)

    result = retrieve_context(
        con, "XYZNOMATCH no keyword hits here",
        semantic=True, semantic_top_n=10,
    )
    ids = [e["id"] for e in result["matched_entities"]]
    assert 3 in ids, "semantic pass must inject entity 3 as a seed"
    assert 3 in result["semantic_seeds"]
    assert result["retrieval_method"] == "hybrid"


def test_retrieve_context_semantic_on_no_embeddings_is_noop(tmp_path):
    """With entity_embeddings empty, semantic=True must degrade to behave like
    keyword-only (no crash, no extra seeds). Only difference: retrieval_method
    is 'hybrid' (the flag was set by the caller)."""
    from extract.retrieval import retrieve_context

    con, _ = _fresh_db(tmp_path)
    _seed_three_entities(con)

    result = retrieve_context(con, "Anna", semantic=True)
    assert result["semantic_seeds"] == []
    names = [e["canonical_name"] for e in result["matched_entities"]]
    assert "Anna" in names


# ---------------------------------------------------------------------------
# run_consolidation(embed_model=...) plumbing
# ---------------------------------------------------------------------------

def test_run_consolidation_with_embed_model_generates_embeddings(tmp_path):
    from pulse_consolidate import run_consolidation

    con, db_path = _fresh_db(tmp_path)
    _seed_three_entities(con)
    con.commit()  # sqlite3 default isolation needs explicit commit
    con.close()

    report = run_consolidation(db_path, embed_model="fake-local")
    assert report.get("embeddings_upserted") == 3
    assert report.get("embed_model") == "fake-local"

    con = sqlite3.connect(db_path)
    n_rows = con.execute("SELECT COUNT(*) FROM entity_embeddings").fetchone()[0]
    assert n_rows == 3


def test_run_consolidation_default_skips_embedding(tmp_path):
    """embed_model=None (default) must not touch entity_embeddings — zero
    behaviour regression vs pre-Phase-3."""
    from pulse_consolidate import run_consolidation

    con, db_path = _fresh_db(tmp_path)
    _seed_three_entities(con)
    con.commit()
    con.close()

    report = run_consolidation(db_path)
    assert report.get("embeddings_upserted") == 0
    assert report.get("embed_model") is None

    con = sqlite3.connect(db_path)
    n_rows = con.execute("SELECT COUNT(*) FROM entity_embeddings").fetchone()[0]
    assert n_rows == 0
