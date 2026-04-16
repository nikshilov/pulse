"""Tests for Pulse graph consolidation."""

import json
import sqlite3
import time
from pathlib import Path

import pytest


def _fresh_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    migrations_dir = Path(__file__).resolve().parent.parent.parent / "internal" / "store" / "migrations"
    for sql_file in sorted(migrations_dir.glob("*.sql")):
        con.executescript(sql_file.read_text())
    return con, str(tmp_path / "test.db")


def test_find_duplicate_candidates_detects_similar_names(tmp_path):
    from pulse_consolidate import find_duplicate_candidates
    con, _ = _fresh_db(tmp_path)
    now = "2026-04-16T00:00:00Z"
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (1,'Anna','person',?,?)", (now, now))
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (2,'Анна','person',?,?)", (now, now))
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (3,'Pulse','project',?,?)", (now, now))

    dupes = find_duplicate_candidates(con)
    assert len(dupes) == 0  # "Anna" vs "Анна" — different scripts, SequenceMatcher < 0.8


def test_find_duplicate_candidates_catches_close_names(tmp_path):
    from pulse_consolidate import find_duplicate_candidates
    con, _ = _fresh_db(tmp_path)
    now = "2026-04-16T00:00:00Z"
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (1,'Alexander','person',?,?)", (now, now))
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (2,'Aleksander','person',?,?)", (now, now))

    dupes = find_duplicate_candidates(con)
    assert len(dupes) == 1
    assert dupes[0]["similarity"] >= 0.8


def test_find_duplicates_ignores_different_kinds(tmp_path):
    from pulse_consolidate import find_duplicate_candidates
    con, _ = _fresh_db(tmp_path)
    now = "2026-04-16T00:00:00Z"
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (1,'Pulse','project',?,?)", (now, now))
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (2,'Pulse','product',?,?)", (now, now))

    dupes = find_duplicate_candidates(con)
    assert len(dupes) == 0


def test_close_stale_questions(tmp_path):
    from pulse_consolidate import close_stale_questions
    con, _ = _fresh_db(tmp_path)
    now = "2026-04-16T00:00:00Z"
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (1,'X','person',?,?)", (now, now))
    past = "2020-01-01T00:00:00Z"
    future = "2099-01-01T00:00:00Z"
    con.execute("INSERT INTO open_questions (subject_entity_id, question_text, asked_at, ttl_expires_at, state) VALUES (1,'stale?',?,?,'open')", (now, past))
    con.execute("INSERT INTO open_questions (subject_entity_id, question_text, asked_at, ttl_expires_at, state) VALUES (1,'fresh?',?,?,'open')", (now, future))

    closed = close_stale_questions(con)
    assert closed == 1
    states = [r[0] for r in con.execute("SELECT state FROM open_questions ORDER BY id").fetchall()]
    assert states == ["auto_closed", "open"]


def test_entity_stats(tmp_path):
    from pulse_consolidate import entity_stats
    con, _ = _fresh_db(tmp_path)
    now = "2026-04-16T00:00:00Z"
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (1,'A','person',?,?)", (now, now))
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (2,'B','person',?,?)", (now, now))
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (3,'C','project',?,?)", (now, now))
    con.execute("INSERT INTO relations (from_entity_id, to_entity_id, kind, strength, first_seen, last_seen) VALUES (1,2,'friend',1.0,?,?)", (now, now))
    con.execute("INSERT INTO facts (entity_id, text, confidence, created_at) VALUES (1,'fact 1',0.9,?)", (now,))

    stats = entity_stats(con)
    assert stats["total_entities"] == 3
    assert stats["entities_by_kind"]["person"] == 2
    assert stats["entities_by_kind"]["project"] == 1
    assert stats["orphan_entities"] == 1  # entity 3 has no relations or facts
    assert stats["total_relations"] == 1
    assert stats["total_facts"] == 1


def test_process_approved_merges(tmp_path):
    from pulse_consolidate import process_approved_merges
    con, _ = _fresh_db(tmp_path)
    now = "2026-04-16T00:00:00Z"
    con.execute("INSERT INTO entities (id, canonical_name, kind, aliases, first_seen, last_seen) VALUES (1,'Anna','person',?,?,?)", (json.dumps(["Аня"]), now, now))
    con.execute("INSERT INTO entities (id, canonical_name, kind, aliases, first_seen, last_seen) VALUES (2,'Анна','person','[]',?,?)", (now, now))
    con.execute("INSERT INTO facts (entity_id, text, confidence, created_at) VALUES (2,'Fact about Анна',0.8,?)", (now,))
    con.execute("INSERT INTO entity_merge_proposals (from_entity_id, to_entity_id, confidence, evidence_md, state, proposed_at) VALUES (2,1,0.95,'similar names','approved',?)", (now,))

    merged = process_approved_merges(con)
    assert merged == 1

    # from_entity (id=2) should be deleted
    assert con.execute("SELECT COUNT(*) FROM entities WHERE id=2").fetchone()[0] == 0
    # fact repointed to entity 1
    assert con.execute("SELECT entity_id FROM facts WHERE text='Fact about Анна'").fetchone()[0] == 1
    # aliases merged
    aliases = json.loads(con.execute("SELECT aliases FROM entities WHERE id=1").fetchone()[0])
    assert "Анна" in aliases


def test_run_consolidation_end_to_end(tmp_path):
    from pulse_consolidate import run_consolidation
    _, db_path = _fresh_db(tmp_path)
    report = run_consolidation(db_path)
    assert "stats" in report
    assert "duplicate_candidates" in report
    assert "stale_questions_closed" in report
    assert "merges_executed" in report
