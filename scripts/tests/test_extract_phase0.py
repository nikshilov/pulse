"""Phase 0 unblock — tests for per-observation tx isolation and PRAGMA fixes."""

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pulse_extract

MOCK_USAGE = {"input_tokens": 0, "output_tokens": 0, "model": "test"}

MIGRATIONS = Path(__file__).resolve().parents[2] / "internal" / "store" / "migrations"


def _apply_migrations(db_path: Path) -> None:
    con = sqlite3.connect(db_path)
    for mig in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(mig.read_text())
    con.commit()
    con.close()


def test_open_connection_sets_pragmas(tmp_path):
    db = tmp_path / "p0.db"
    _apply_migrations(db)

    con = pulse_extract._open_connection(str(db))
    try:
        fk = con.execute("PRAGMA foreign_keys").fetchone()[0]
        bt = con.execute("PRAGMA busy_timeout").fetchone()[0]
        isolation = con.isolation_level
    finally:
        con.close()

    assert fk == 1, "foreign_keys must be ON"
    assert bt == 5000, "busy_timeout must be 5000 ms"
    assert isolation is None, (
        "isolation_level must be None so BEGIN/COMMIT are under our control"
    )


def test_apply_extraction_returns_report(tmp_path):
    db = tmp_path / "p0.db"
    _apply_migrations(db)

    con = pulse_extract._open_connection(str(db))
    # seed one observation so the evidence FK resolves
    con.execute(
        """INSERT INTO observations
           (source_kind, source_id, content_hash, version, scope, captured_at,
            observed_at, actors, content_text, metadata, raw_json)
           VALUES ('claude_jsonl','f:1','h1',1,'shared','2026-04-15T00:00:00Z',
                   '2026-04-15T00:00:00Z','[]','hello','{}','{}')"""
    )
    con.execute("BEGIN IMMEDIATE")
    result = {
        "entities": [{"canonical_name": "Anna", "kind": "person", "aliases": ["Аня"]}],
        "events": [],
        "relations": [],
        "facts": [],
    }
    report = pulse_extract._apply_extraction(con, 1, result)
    con.execute("COMMIT")
    con.close()

    assert report["obs_id"] == 1
    assert report["entities_written"] == 1
    assert report["events_written"] == 0
    assert report["relations_written"] == 0
    assert report["facts_written"] == 0
    assert report["failed_items"] == []


def test_event_without_entities_involved_is_dropped(tmp_path):
    db = tmp_path / "p0.db"
    _apply_migrations(db)

    con = pulse_extract._open_connection(str(db))
    con.execute(
        """INSERT INTO observations
           (source_kind, source_id, content_hash, version, scope, captured_at,
            observed_at, actors, content_text, metadata, raw_json)
           VALUES ('claude_jsonl','f:1','h1',1,'shared','2026-04-15T00:00:00Z',
                   '2026-04-15T00:00:00Z','[]','hello','{}','{}')"""
    )
    con.execute("BEGIN IMMEDIATE")
    result = {
        "entities": [{"canonical_name": "Anna", "kind": "person"}],
        "events": [
            # Orphan — no entities_involved, must be skipped
            {"title": "birthday", "description": "party", "sentiment": 0.5, "emotional_weight": 0.3},
            # Valid — entities_involved present, must be written
            {"title": "meeting", "description": "work", "sentiment": 0.0, "emotional_weight": 0.2,
             "entities_involved": ["Anna"]},
        ],
        "relations": [],
        "facts": [],
    }
    report = pulse_extract._apply_extraction(con, 1, result)
    con.execute("COMMIT")

    assert report["events_written"] == 1, "only the event with entities_involved counts"
    orphan_failures = [f for f in report["failed_items"] if f["reason"] == "orphan_event_no_entities_involved"]
    assert len(orphan_failures) == 1
    assert orphan_failures[0]["detail"]["title"] == "birthday"

    db_events = con.execute("SELECT title FROM events").fetchall()
    con.close()
    assert [r[0] for r in db_events] == ["meeting"]


def test_savepoint_isolates_one_bad_entity(tmp_path):
    """If entity 2 of 3 violates a CHECK constraint, entities 1 and 3 must still be written."""
    db = tmp_path / "p0.db"
    _apply_migrations(db)

    con = pulse_extract._open_connection(str(db))
    con.execute(
        """INSERT INTO observations
           (source_kind, source_id, content_hash, version, scope, captured_at,
            observed_at, actors, content_text, metadata, raw_json)
           VALUES ('claude_jsonl','f:1','h1',1,'shared','2026-04-15T00:00:00Z',
                   '2026-04-15T00:00:00Z','[]','hello','{}','{}')"""
    )
    con.execute("BEGIN IMMEDIATE")
    result = {
        "entities": [
            {"canonical_name": "Anna", "kind": "person"},
            # kind='potato' violates CHECK (kind IN (...)) — must be skipped, not abort
            {"canonical_name": "Bad", "kind": "potato"},
            {"canonical_name": "Fedya", "kind": "person"},
        ],
        "events": [],
        "relations": [],
        "facts": [],
    }
    report = pulse_extract._apply_extraction(con, 1, result)
    con.execute("COMMIT")

    assert report["entities_written"] == 2, "good entities (Anna, Fedya) must both commit"
    bad = [f for f in report["failed_items"] if f["item_kind"] == "entity"]
    assert len(bad) == 1
    assert bad[0]["detail"]["canonical_name"] == "Bad"

    names = {r[0] for r in con.execute("SELECT canonical_name FROM entities")}
    con.close()
    assert names == {"Anna", "Fedya"}


def test_savepoint_isolates_one_bad_relation(tmp_path):
    """If relation 1 of 3 violates a NOT NULL constraint (kind=None), relations 0 and 2
    must still be written, and failed_items must record index=1."""
    db = tmp_path / "p0.db"
    _apply_migrations(db)

    con = pulse_extract._open_connection(str(db))
    con.execute(
        """INSERT INTO observations
           (source_kind, source_id, content_hash, version, scope, captured_at,
            observed_at, actors, content_text, metadata, raw_json)
           VALUES ('claude_jsonl','f:1','h1',1,'shared','2026-04-15T00:00:00Z',
                   '2026-04-15T00:00:00Z','[]','hello','{}','{}')"""
    )
    con.execute("BEGIN IMMEDIATE")
    result = {
        "entities": [
            {"canonical_name": "Anna", "kind": "person"},
            {"canonical_name": "Fedya", "kind": "person"},
        ],
        "events": [],
        "relations": [
            {"from": "Anna", "to": "Fedya", "kind": "friend", "strength": 0.8},
            # kind=None violates NOT NULL on relations.kind — must be skipped, not abort
            {"from": "Anna", "to": "Fedya", "kind": None, "strength": 0.5},
            {"from": "Fedya", "to": "Anna", "kind": "friend", "strength": 0.8},
        ],
        "facts": [],
    }
    report = pulse_extract._apply_extraction(con, 1, result)
    con.execute("COMMIT")

    assert report["relations_written"] == 2, "good relations (idx 0 and 2) must both commit"
    bad = [f for f in report["failed_items"] if f["item_kind"] == "relation"]
    assert len(bad) == 1
    assert bad[0]["detail"]["index"] == 1
    assert bad[0]["item_kind"] == "relation"

    rel_kinds = [r[0] for r in con.execute("SELECT kind FROM relations ORDER BY id")]
    con.close()
    assert rel_kinds == ["friend", "friend"]


def test_crash_in_obs_two_preserves_obs_one_writes(tmp_path, monkeypatch):
    """A RuntimeError in the middle of obs 2's apply must not roll back obs 1's writes."""
    db = tmp_path / "p0.db"
    _apply_migrations(db)

    con = sqlite3.connect(db)
    con.execute(
        """INSERT INTO observations
           (source_kind, source_id, content_hash, version, scope, captured_at,
            observed_at, actors, content_text, metadata, raw_json)
           VALUES ('claude_jsonl','f:1','h1',1,'shared','2026-04-15T00:00:00Z',
                   '2026-04-15T00:00:00Z','[]','Anna said hi','{}','{}')"""
    )
    con.execute(
        """INSERT INTO observations
           (source_kind, source_id, content_hash, version, scope, captured_at,
            observed_at, actors, content_text, metadata, raw_json)
           VALUES ('claude_jsonl','f:2','h2',1,'shared','2026-04-15T00:00:00Z',
                   '2026-04-15T00:00:00Z','[]','Fedya ran','{}','{}')"""
    )
    con.execute(
        """INSERT INTO extraction_jobs
           (observation_ids, state, attempts, created_at, updated_at)
           VALUES ('[1,2]', 'pending', 0,
                   '2026-04-15T00:00:00Z', '2026-04-15T00:00:00Z')"""
    )
    con.commit()
    con.close()

    # Triage: both obs return "extract"
    def fake_triage(_prompt, expected_count):
        return ([{"verdict": "extract"} for _ in range(expected_count)], MOCK_USAGE)

    # Extract for obs 1 writes Anna; extract for obs 2 raises RuntimeError
    call_count = {"n": 0}

    def fake_extract(_prompt):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return (
                {
                    "entities": [{"canonical_name": "Anna", "kind": "person"}],
                    "events": [], "relations": [], "facts": [],
                },
                MOCK_USAGE,
            )
        raise RuntimeError("simulated Anthropic timeout")

    monkeypatch.setattr(pulse_extract, "call_sonnet_triage", fake_triage)
    monkeypatch.setattr(pulse_extract, "call_opus_extract", fake_extract)

    rc = pulse_extract.run_once(str(db))
    assert rc == 0

    con = sqlite3.connect(db)
    names = {r[0] for r in con.execute("SELECT canonical_name FROM entities")}
    assert names == {"Anna"}, "obs 1's Anna must survive obs 2's crash"
    anna_count = con.execute("SELECT COUNT(*) FROM entities WHERE canonical_name='Anna'").fetchone()[0]
    assert anna_count == 1, "Anna must appear exactly once (no duplicate from at-least-once re-apply)"

    job_state = con.execute("SELECT state, last_error FROM extraction_jobs WHERE id=1").fetchone()
    con.close()
    assert job_state[0] in ("pending", "dlq"), "job must retry or DLQ, not 'done'"
    assert job_state[1] is not None and "simulated" in job_state[1]


def test_job_state_running_commits_before_apply(tmp_path, monkeypatch):
    """The state transition to 'running' must commit before any apply writes,
    so a crash mid-apply can't rewind the state update."""
    db = tmp_path / "p0.db"
    _apply_migrations(db)

    con = sqlite3.connect(db)
    con.execute(
        """INSERT INTO observations
           (source_kind, source_id, content_hash, version, scope, captured_at,
            observed_at, actors, content_text, metadata, raw_json)
           VALUES ('claude_jsonl','f:1','h1',1,'shared','2026-04-15T00:00:00Z',
                   '2026-04-15T00:00:00Z','[]','hi','{}','{}')"""
    )
    con.execute(
        """INSERT INTO extraction_jobs
           (observation_ids, state, attempts, created_at, updated_at)
           VALUES ('[1]', 'pending', 0,
                   '2026-04-15T00:00:00Z', '2026-04-15T00:00:00Z')"""
    )
    con.commit()
    con.close()

    def fake_triage(_prompt, expected_count):
        return ([{"verdict": "extract"}], MOCK_USAGE)

    # Capture job state at the moment extract is called
    captured = {}

    def fake_extract(_prompt):
        probe = sqlite3.connect(db)
        probe.execute("PRAGMA busy_timeout=2000")
        row = probe.execute("SELECT state, attempts FROM extraction_jobs WHERE id=1").fetchone()
        probe.close()
        captured["state_mid"] = row[0]
        captured["attempts_mid"] = row[1]
        raise RuntimeError("boom")

    monkeypatch.setattr(pulse_extract, "call_sonnet_triage", fake_triage)
    monkeypatch.setattr(pulse_extract, "call_opus_extract", fake_extract)

    pulse_extract.run_once(str(db))

    assert captured["state_mid"] == "running", "state must be 'running' during apply"
    assert captured["attempts_mid"] == 1, "attempts must be incremented before apply"
