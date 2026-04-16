"""Tests for keyword-based graph retrieval."""

import json
import sqlite3
from pathlib import Path

import pytest


def _fresh_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    migrations_dir = Path(__file__).resolve().parent.parent.parent / "internal" / "store" / "migrations"
    for sql_file in sorted(migrations_dir.glob("*.sql")):
        con.executescript(sql_file.read_text())
    return con


def _seed_graph(con):
    """Seed a small graph: Anna (person), Nik (person), Pulse (project), with relations and facts."""
    now = "2026-04-16T00:00:00Z"
    con.execute("INSERT INTO entities (id, canonical_name, kind, aliases, first_seen, last_seen, salience_score) VALUES (1,'Anna','person',?,?,?,0.9)", (json.dumps(["Аня", "Анна"]), now, now))
    con.execute("INSERT INTO entities (id, canonical_name, kind, aliases, first_seen, last_seen, salience_score) VALUES (2,'Nik','person',?,?,?,1.0)", (json.dumps(["Никита"]), now, now))
    con.execute("INSERT INTO entities (id, canonical_name, kind, aliases, first_seen, last_seen, salience_score) VALUES (3,'Pulse','project',?,?,?,0.8)", (json.dumps(["pulse-engine"]), now, now))
    con.execute("INSERT INTO relations (from_entity_id, to_entity_id, kind, strength, context, first_seen, last_seen) VALUES (1,2,'spouse',1.0,'married since 2020',?,?)", (now, now))
    con.execute("INSERT INTO relations (from_entity_id, to_entity_id, kind, strength, first_seen, last_seen) VALUES (2,3,'creator',1.0,?,?)", (now, now))
    con.execute("INSERT INTO facts (entity_id, text, confidence, created_at) VALUES (1,'Loves cats',0.9,?)", (now,))
    con.execute("INSERT INTO facts (entity_id, text, confidence, created_at) VALUES (3,'Written in Go and Python',0.95,?)", (now,))


def test_retrieve_by_canonical_name(tmp_path):
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    _seed_graph(con)
    result = retrieve_context(con, "Давай обсудим Anna")
    assert result["total_matched"] >= 1
    names = [e["canonical_name"] for e in result["matched_entities"]]
    assert "Anna" in names


def test_retrieve_by_alias(tmp_path):
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    _seed_graph(con)
    result = retrieve_context(con, "Аня сегодня устала")
    names = [e["canonical_name"] for e in result["matched_entities"]]
    assert "Anna" in names


def test_retrieve_includes_relations(tmp_path):
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    _seed_graph(con)
    result = retrieve_context(con, "Anna")
    anna = [e for e in result["matched_entities"] if e["canonical_name"] == "Anna"][0]
    rel_kinds = [r["kind"] for r in anna["relations"]]
    assert "spouse" in rel_kinds


def test_retrieve_includes_facts(tmp_path):
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    _seed_graph(con)
    result = retrieve_context(con, "Anna")
    anna = [e for e in result["matched_entities"] if e["canonical_name"] == "Anna"][0]
    fact_texts = [f["text"] for f in anna["facts"]]
    assert "Loves cats" in fact_texts


def test_retrieve_respects_top_k(tmp_path):
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    _seed_graph(con)
    result = retrieve_context(con, "Anna Nik Pulse", top_k=2)
    assert len(result["matched_entities"]) <= 2


def test_retrieve_no_match_returns_empty(tmp_path):
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    _seed_graph(con)
    result = retrieve_context(con, "XYZNONEXISTENT")
    assert result["total_matched"] == 0
    assert result["matched_entities"] == []


def test_retrieve_method_is_keyword(tmp_path):
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    _seed_graph(con)
    result = retrieve_context(con, "Anna")
    assert result["retrieval_method"] == "keyword"


def test_retrieve_2hop_indirect_relation(tmp_path):
    """Anna→Nik→Pulse: querying 'Anna' with depth=2 should find Pulse via Nik."""
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    _seed_graph(con)
    result = retrieve_context(con, "Anna", depth=2)
    names = [e["canonical_name"] for e in result["matched_entities"]]
    assert "Anna" in names
    assert "Pulse" in names  # found via Anna→Nik→Pulse (2 hops)


def test_hop_penalty_ranking(tmp_path):
    """Direct match should rank above 2-hop match."""
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    _seed_graph(con)
    result = retrieve_context(con, "Anna", depth=2)
    entities = result["matched_entities"]
    anna_idx = next(i for i, e in enumerate(entities) if e["canonical_name"] == "Anna")
    pulse_idx = next(i for i, e in enumerate(entities) if e["canonical_name"] == "Pulse")
    assert anna_idx < pulse_idx  # Anna (direct) ranks above Pulse (2 hops away)


def test_depth_0_returns_only_matched(tmp_path):
    """depth=0 returns matched entity without expanding relations."""
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    _seed_graph(con)
    result = retrieve_context(con, "Anna", depth=0)
    names = [e["canonical_name"] for e in result["matched_entities"]]
    assert "Anna" in names
    assert "Nik" not in names  # no expansion
