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


# ---------------------------------------------------------------------------
# Task 3: Skip-guard
# ---------------------------------------------------------------------------

def test_skip_guard_skips_when_no_changes(tmp_path):
    from pulse_consolidate import run_consolidation
    _, db_path = _fresh_db(tmp_path)
    report1 = run_consolidation(db_path)
    assert report1.get("skipped") is not True
    report2 = run_consolidation(db_path)
    assert report2["skipped"] is True
    assert "reason" in report2


def test_skip_guard_runs_when_new_entities(tmp_path):
    from pulse_consolidate import run_consolidation
    con, db_path = _fresh_db(tmp_path)
    run_consolidation(db_path)
    now = "2099-01-01T00:00:00Z"
    con.execute("INSERT INTO entities (canonical_name, kind, first_seen, last_seen) VALUES ('NewPerson','person',?,?)", (now, now))
    con.commit()
    report2 = run_consolidation(db_path)
    assert report2.get("skipped") is not True


# ---------------------------------------------------------------------------
# Task 4: Co-occurrence Detection
# ---------------------------------------------------------------------------

def test_find_cooccurrence_candidates(tmp_path):
    from pulse_consolidate import find_cooccurrence_candidates
    con, _ = _fresh_db(tmp_path)
    now = "2026-04-16T00:00:00Z"
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (1,'Anna','person',?,?)", (now, now))
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (2,'Nik','person',?,?)", (now, now))
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (3,'Solo','person',?,?)", (now, now))
    for i in range(1, 4):
        con.execute("INSERT INTO events (id, title, ts) VALUES (?,?,?)", (i, f"event_{i}", now))
        con.execute("INSERT INTO event_entities (event_id, entity_id) VALUES (?,?)", (i, 1))
        con.execute("INSERT INTO event_entities (event_id, entity_id) VALUES (?,?)", (i, 2))
    con.execute("INSERT INTO event_entities (event_id, entity_id) VALUES (1,3)")
    candidates = find_cooccurrence_candidates(con)
    pairs = [(c["entity_a_id"], c["entity_b_id"]) for c in candidates]
    assert (1, 2) in pairs
    assert all(c["co_count"] >= 3 for c in candidates)


# ---------------------------------------------------------------------------
# Task 5: Knowledge Gaps + Auto-populate open_questions
# ---------------------------------------------------------------------------

def test_detect_knowledge_gaps(tmp_path):
    from pulse_consolidate import detect_knowledge_gaps
    con, _ = _fresh_db(tmp_path)
    now = "2026-04-16T00:00:00Z"
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (1,'MysteryPerson','person',?,?)", (now, now))
    for i in range(1, 6):
        con.execute("INSERT INTO events (id, title, ts) VALUES (?,?,?)", (i, f"event_{i}", now))
        con.execute("INSERT INTO event_entities (event_id, entity_id) VALUES (?,1)", (i,))
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (2,'Rare','person',?,?)", (now, now))
    con.execute("INSERT INTO events (id, title, ts) VALUES (6,'ev6',?)", (now,))
    con.execute("INSERT INTO event_entities (event_id, entity_id) VALUES (6,2)")
    gaps = detect_knowledge_gaps(con)
    gap_ids = [g["entity_id"] for g in gaps]
    assert 1 in gap_ids
    assert 2 not in gap_ids


def test_auto_populate_questions(tmp_path):
    from pulse_consolidate import detect_knowledge_gaps, auto_populate_questions
    con, _ = _fresh_db(tmp_path)
    now = "2026-04-16T00:00:00Z"
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (1,'Gap','person',?,?)", (now, now))
    for i in range(1, 5):
        con.execute("INSERT INTO events (id, title, ts) VALUES (?,?,?)", (i, f"e{i}", now))
        con.execute("INSERT INTO event_entities (event_id, entity_id) VALUES (?,1)", (i,))
    gaps = detect_knowledge_gaps(con)
    added = auto_populate_questions(con, gaps)
    assert added >= 1
    questions = con.execute("SELECT question_text, state FROM open_questions WHERE subject_entity_id=1").fetchall()
    assert len(questions) >= 1
    assert questions[0][1] == "open"


# ---------------------------------------------------------------------------
# Safety gates (migration 009 — do_not_probe + emotional_weight)
# ---------------------------------------------------------------------------

def _seed_gap_entity(con, entity_id, name, emotional_weight=0.0, do_not_probe=0, mentions=5):
    """Helper: entity with enough mentions + zero facts to trip knowledge_gaps."""
    now = "2026-04-16T00:00:00Z"
    con.execute(
        "INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen, "
        "                      emotional_weight, do_not_probe) "
        "VALUES (?, ?, 'person', ?, ?, ?, ?)",
        (entity_id, name, now, now, emotional_weight, do_not_probe),
    )
    for i in range(mentions):
        # unique event ids per entity to avoid PK collisions across helper calls
        eid = entity_id * 1000 + i
        con.execute("INSERT INTO events (id, title, ts) VALUES (?, ?, ?)", (eid, f"e{eid}", now))
        con.execute("INSERT INTO event_entities (event_id, entity_id) VALUES (?, ?)", (eid, entity_id))


def test_auto_question_skips_emotionally_heavy_entity(tmp_path):
    """Entity with emotional_weight=0.8 (Anna/Kristina-class) must NOT get an auto-question.

    Even if it structurally qualifies (≥3 mentions, ≤1 fact) the emotion gate blocks it.
    Elle does not initiate about wounds on a random Tuesday.
    """
    from pulse_consolidate import detect_knowledge_gaps, auto_populate_questions
    con, _ = _fresh_db(tmp_path)
    _seed_gap_entity(con, 1, "Kristina", emotional_weight=0.8, do_not_probe=0, mentions=5)

    gaps = detect_knowledge_gaps(con)
    # Gap is detected (structurally valid) …
    assert any(g["entity_id"] == 1 for g in gaps)
    # … but auto_populate_questions skips it on the emotion gate.
    added = auto_populate_questions(con, gaps)
    assert added == 0
    rows = con.execute("SELECT COUNT(*) FROM open_questions WHERE subject_entity_id = 1").fetchone()[0]
    assert rows == 0


def test_auto_question_skips_do_not_probe_entity(tmp_path):
    """Entity with do_not_probe=1 must NOT appear in knowledge_gaps at all (SQL-level gate)."""
    from pulse_consolidate import detect_knowledge_gaps, auto_populate_questions
    con, _ = _fresh_db(tmp_path)
    _seed_gap_entity(con, 1, "OptedOut", emotional_weight=0.0, do_not_probe=1, mentions=5)

    gaps = detect_knowledge_gaps(con)
    assert all(g["entity_id"] != 1 for g in gaps)

    # Even if the filter were bypassed, auto_populate_questions on an empty gap list
    # must create nothing. Belt and suspenders.
    added = auto_populate_questions(con, gaps)
    assert added == 0
    rows = con.execute("SELECT COUNT(*) FROM open_questions WHERE subject_entity_id = 1").fetchone()[0]
    assert rows == 0


# ---------------------------------------------------------------------------
# Task 7: Observability Metrics
# ---------------------------------------------------------------------------

def test_valence_trend_stable(tmp_path):
    from pulse_consolidate import valence_trend
    con, _ = _fresh_db(tmp_path)
    for i in range(10):
        day = f"2026-04-{i+1:02d}T12:00:00Z"
        con.execute("INSERT INTO events (title, sentiment, ts) VALUES (?,?,?)", (f"e{i}", 0.5, day))
    result = valence_trend(con, days=30)
    assert result["trend"] in ("stable", "no_data", "insufficient")
    assert result["data_points"] >= 2


def test_extraction_efficiency(tmp_path):
    from pulse_consolidate import extraction_efficiency
    con, _ = _fresh_db(tmp_path)
    now = "2026-04-16T00:00:00Z"
    for i in range(1, 6):
        con.execute("INSERT INTO entities (canonical_name, kind, first_seen, last_seen) VALUES (?,?,?,?)", (f"E{i}", "person", now, now))
    con.execute(
        "INSERT INTO extraction_jobs (observation_ids, state, created_at, updated_at) VALUES ('[1]','done',?,?)",
        (now, now),
    )
    con.execute("INSERT INTO extraction_metrics (job_id, model, input_tokens, output_tokens) VALUES (1,'opus',5000,1000)")
    result = extraction_efficiency(con)
    assert result["total_entities"] == 5
    assert result["total_tokens"] == 6000
    assert result["entities_per_1k_tokens"] > 0


def test_merge_survives_corrupted_aliases_json(tmp_path):
    """process_approved_merges should not crash on invalid aliases JSON."""
    from pulse_consolidate import process_approved_merges
    con, _ = _fresh_db(tmp_path)
    now = "2026-04-16T00:00:00Z"
    con.execute(
        "INSERT INTO entities (id, canonical_name, kind, aliases, first_seen, last_seen, salience_score) "
        "VALUES (1,'OldName','person','BROKEN JSON',?,?,0.5)", (now, now)
    )
    con.execute(
        "INSERT INTO entities (id, canonical_name, kind, aliases, first_seen, last_seen, salience_score) "
        "VALUES (2,'NewName','person','ALSO BROKEN',?,?,0.8)", (now, now)
    )
    con.execute(
        "INSERT INTO entity_merge_proposals (from_entity_id, to_entity_id, confidence, evidence_md, state, proposed_at) "
        "VALUES (1, 2, 0.95, 'test', 'approved', ?)", (now,)
    )
    merged = process_approved_merges(con)
    assert merged == 1
    remaining = con.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    assert remaining == 1


def test_run_consolidation_atomicity(tmp_path):
    """run_consolidation wraps mutations in a transaction."""
    from pulse_consolidate import run_consolidation
    _, db_path = _fresh_db(tmp_path)
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    now = "2026-04-16T00:00:00Z"
    con.execute(
        "INSERT INTO entities (canonical_name, kind, first_seen, last_seen, salience_score) "
        "VALUES ('TestEntity','person',?,?,0.8)", (now, now)
    )
    con.commit()
    con.execute(
        "INSERT INTO open_questions (subject_entity_id, question_text, asked_at, ttl_expires_at, state) "
        "VALUES (1, 'stale?', ?, '2020-01-01T00:00:00Z', 'open')", (now,)
    )
    con.commit()
    con.close()
    report = run_consolidation(db_path)
    assert report["stale_questions_closed"] == 1
