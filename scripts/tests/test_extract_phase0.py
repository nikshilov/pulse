"""Phase 0 unblock — tests for per-observation tx isolation and PRAGMA fixes."""

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pulse_extract
from extract import prompts

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
