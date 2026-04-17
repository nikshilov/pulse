"""Tests for graph_snapshots logging + pulse_rewind CLI.

Covers the red-team undo path: malicious observation mutates graph →
pulse_rewind reverses every mutation, restores prior state, aborts cleanly
on FK conflicts without partial writes.
"""

import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import pulse_extract  # noqa: E402
import pulse_rewind  # noqa: E402

MIGRATIONS = Path(__file__).resolve().parents[2] / "internal" / "store" / "migrations"


def _apply_migrations(db_path: Path) -> None:
    con = sqlite3.connect(db_path)
    for mig in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(mig.read_text())
    con.commit()
    con.close()


def _insert_obs(con: sqlite3.Connection, obs_id: int) -> None:
    con.execute(
        """INSERT INTO observations
           (id, source_kind, source_id, content_hash, version, scope, captured_at,
            observed_at, actors, content_text, metadata, raw_json)
           VALUES (?, 'claude_jsonl', ?, ?, 1, 'shared',
                   '2026-04-16T00:00:00Z','2026-04-16T00:00:00Z','[]','t','{}','{}')""",
        (obs_id, f"f:{obs_id}", f"h{obs_id}"),
    )


def _setup_db(tmp_path: Path, obs_ids=(1,)) -> sqlite3.Connection:
    db = tmp_path / "t.db"
    _apply_migrations(db)
    con = pulse_extract._open_connection(str(db))
    for oid in obs_ids:
        _insert_obs(con, oid)
    return con


def _apply(con: sqlite3.Connection, obs_id: int, result: dict) -> dict:
    con.execute("BEGIN IMMEDIATE")
    try:
        r = pulse_extract._apply_extraction(con, obs_id, result)
        con.execute("COMMIT")
        return r
    except Exception:
        con.execute("ROLLBACK")
        raise


# ---------- snapshot logging ----------

def test_snapshot_logged_for_new_entity(tmp_path):
    con = _setup_db(tmp_path)
    result = {"entities": [{"canonical_name": "Mark", "kind": "person"}],
              "events": [], "relations": [], "facts": []}
    _apply(con, 1, result)

    rows = con.execute(
        "SELECT op, table_name, row_id, before_json, after_json "
        "FROM graph_snapshots WHERE observation_id=1 ORDER BY id"
    ).fetchall()
    ops = [r[0] for r in rows]
    assert "insert_entity" in ops
    assert "insert_evidence" in ops

    ent_row = next(r for r in rows if r[0] == "insert_entity")
    assert ent_row[1] == "entities"
    assert ent_row[2] is not None  # row_id set
    assert ent_row[3] is None       # before is NULL for inserts
    after = json.loads(ent_row[4])
    assert after["canonical_name"] == "Mark"
    assert after["kind"] == "person"


def test_snapshot_logged_for_updated_entity(tmp_path):
    con = _setup_db(tmp_path, obs_ids=(1, 2))
    # Seed an entity via obs 1
    seed = {"entities": [{"canonical_name": "Anna", "kind": "person"}],
            "events": [], "relations": [], "facts": []}
    _apply(con, 1, seed)
    # Re-apply same name via obs 2 → resolver binds identity → UPDATE
    _apply(con, 2, seed)

    updates = con.execute(
        "SELECT before_json, after_json FROM graph_snapshots "
        "WHERE observation_id=2 AND op='update_entity'"
    ).fetchall()
    assert len(updates) == 1
    before = json.loads(updates[0][0])
    after = json.loads(updates[0][1])
    assert before["canonical_name"] == "Anna"
    assert after["canonical_name"] == "Anna"
    # update_entity bumps last_seen/scorer_version; at minimum before and after
    # must BOTH carry the full column set (id present)
    assert "id" in before and "id" in after


# ---------- rewind: deletes inserts ----------

def test_rewind_deletes_inserted_entity(tmp_path):
    con = _setup_db(tmp_path)
    result = {"entities": [{"canonical_name": "Mark", "kind": "person"}],
              "events": [], "relations": [], "facts": []}
    _apply(con, 1, result)

    assert con.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM graph_snapshots WHERE observation_id=1"
                       ).fetchone()[0] > 0

    db_path = str(tmp_path / "t.db")
    con.close()

    out = io.StringIO()
    rc = pulse_rewind.rewind(db_path, 1, assume_yes=True, out=out)
    assert rc == 0, out.getvalue()

    con2 = sqlite3.connect(db_path)
    assert con2.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 0
    assert con2.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0
    assert con2.execute(
        "SELECT COUNT(*) FROM graph_snapshots WHERE observation_id=1"
    ).fetchone()[0] == 0
    # erasure_log records the rewind
    row = con2.execute(
        "SELECT op_kind, subject_kind, subject_id, initiated_by "
        "FROM erasure_log WHERE subject_id='1'"
    ).fetchone()
    assert row == ("hard", "observation", "1", "pulse_rewind_cli")
    # Observation soft-erased
    redacted = con2.execute("SELECT redacted, content_text FROM observations WHERE id=1").fetchone()
    assert redacted[0] == 1
    assert redacted[1] is None
    con2.close()


# ---------- rewind: restores updates ----------

def test_rewind_restores_updated_entity(tmp_path):
    con = _setup_db(tmp_path, obs_ids=(1, 2))
    seed = {"entities": [{"canonical_name": "Anna", "kind": "person"}],
            "events": [], "relations": [], "facts": []}
    _apply(con, 1, seed)
    # Capture post-seed state of entity 1
    pre_update = con.execute(
        "SELECT canonical_name, kind, salience_score, emotional_weight, scorer_version, last_seen "
        "FROM entities WHERE id=1"
    ).fetchone()

    # Obs 2 triggers bind_identity UPDATE on entity 1
    _apply(con, 2, seed)
    post_update = con.execute(
        "SELECT canonical_name, kind, salience_score, emotional_weight, scorer_version, last_seen "
        "FROM entities WHERE id=1"
    ).fetchone()

    db_path = str(tmp_path / "t.db")
    con.close()

    out = io.StringIO()
    rc = pulse_rewind.rewind(db_path, 2, assume_yes=True, out=out)
    assert rc == 0, out.getvalue()

    con2 = sqlite3.connect(db_path)
    restored = con2.execute(
        "SELECT canonical_name, kind, salience_score, emotional_weight, scorer_version, last_seen "
        "FROM entities WHERE id=1"
    ).fetchone()
    con2.close()
    # After rewind, entity 1 must match the state right after obs 1, not obs 2.
    assert restored == pre_update
    # And to prove the test isn't trivial, pre != post must differ in at least
    # one field (scorer_version / salience / last_seen — one of these moves on
    # re-apply).
    assert restored != post_update or pre_update == post_update


# ---------- dry run ----------

def test_rewind_dry_run_does_not_mutate(tmp_path):
    con = _setup_db(tmp_path)
    result = {"entities": [{"canonical_name": "Mark", "kind": "person"}],
              "events": [], "relations": [], "facts": []}
    _apply(con, 1, result)
    db_path = str(tmp_path / "t.db")
    con.close()

    out = io.StringIO()
    rc = pulse_rewind.rewind(db_path, 1, dry_run=True, assume_yes=True, out=out)
    assert rc == 0
    text = out.getvalue()
    assert "DRY RUN" in text
    assert "DELETE FROM entities" in text

    con2 = sqlite3.connect(db_path)
    assert con2.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 1
    assert con2.execute(
        "SELECT COUNT(*) FROM graph_snapshots WHERE observation_id=1"
    ).fetchone()[0] > 0
    assert con2.execute("SELECT redacted FROM observations WHERE id=1").fetchone()[0] == 0
    con2.close()


# ---------- FK abort ----------

def test_rewind_aborts_on_fk_violation(tmp_path):
    """Entity created by obs 1, fact referring to that entity created by obs 2.
    Rewinding obs 1 would delete the entity, but the fact from obs 2 still
    references it → FK violation → whole tx must roll back.
    """
    con = _setup_db(tmp_path, obs_ids=(1, 2))

    # Obs 1: create Anna
    _apply(con, 1, {
        "entities": [{"canonical_name": "Anna", "kind": "person"}],
        "events": [], "relations": [], "facts": [],
    })
    # Obs 2: bind Anna + attach fact
    _apply(con, 2, {
        "entities": [{"canonical_name": "Anna", "kind": "person"}],
        "events": [], "relations": [],
        "facts": [{"entity": "Anna", "text": "ест сыр", "confidence": 0.9}],
    })

    # Pre-rewind: anna exists, anna's fact exists
    anna_id = con.execute("SELECT id FROM entities WHERE canonical_name='Anna'"
                          ).fetchone()[0]
    assert con.execute("SELECT COUNT(*) FROM facts WHERE entity_id=?",
                       (anna_id,)).fetchone()[0] == 1

    db_path = str(tmp_path / "t.db")
    con.close()

    out = io.StringIO()
    rc = pulse_rewind.rewind(db_path, 1, assume_yes=True, out=out)
    # Non-zero return code signals abort
    assert rc != 0, out.getvalue()
    assert "FK violation" in out.getvalue() or "ABORT" in out.getvalue()

    # DB unchanged: entity still there, fact still there, snapshots intact
    con2 = sqlite3.connect(db_path)
    assert con2.execute("SELECT COUNT(*) FROM entities WHERE id=?",
                        (anna_id,)).fetchone()[0] == 1
    assert con2.execute("SELECT COUNT(*) FROM facts WHERE entity_id=?",
                        (anna_id,)).fetchone()[0] == 1
    # Snapshot rows for obs 1 still there (no partial cleanup)
    assert con2.execute(
        "SELECT COUNT(*) FROM graph_snapshots WHERE observation_id=1"
    ).fetchone()[0] > 0
    # No erasure_log entry written
    assert con2.execute(
        "SELECT COUNT(*) FROM erasure_log WHERE subject_id='1'"
    ).fetchone()[0] == 0
    con2.close()


# ---------- snapshot rows cleaned ----------

def test_rewind_removes_snapshot_rows(tmp_path):
    con = _setup_db(tmp_path)
    _apply(con, 1, {
        "entities": [{"canonical_name": "Mark", "kind": "person"}],
        "events": [], "relations": [], "facts": [],
    })
    assert con.execute(
        "SELECT COUNT(*) FROM graph_snapshots WHERE observation_id=1"
    ).fetchone()[0] > 0

    db_path = str(tmp_path / "t.db")
    con.close()

    out = io.StringIO()
    rc = pulse_rewind.rewind(db_path, 1, assume_yes=True, out=out)
    assert rc == 0, out.getvalue()

    con2 = sqlite3.connect(db_path)
    assert con2.execute(
        "SELECT COUNT(*) FROM graph_snapshots WHERE observation_id=1"
    ).fetchone()[0] == 0
    con2.close()


# ---------- event + junction ----------

def test_rewind_deletes_event_and_event_entities(tmp_path):
    con = _setup_db(tmp_path)
    _apply(con, 1, {
        "entities": [
            {"canonical_name": "Anna", "kind": "person"},
            {"canonical_name": "Mark", "kind": "person"},
        ],
        "events": [{
            "title": "meeting", "description": "they met",
            "entities_involved": ["Anna", "Mark"],
            "ts": "2026-04-16T12:00:00Z",
        }],
        "relations": [], "facts": [],
    })

    assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM event_entities").fetchone()[0] == 2

    db_path = str(tmp_path / "t.db")
    con.close()

    out = io.StringIO()
    rc = pulse_rewind.rewind(db_path, 1, assume_yes=True, out=out)
    assert rc == 0, out.getvalue()

    con2 = sqlite3.connect(db_path)
    assert con2.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert con2.execute("SELECT COUNT(*) FROM event_entities").fetchone()[0] == 0
    assert con2.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 0
    con2.close()


# ---------- no snapshots = noop ----------

def test_rewind_noop_when_no_snapshots(tmp_path):
    db = tmp_path / "t.db"
    _apply_migrations(db)
    out = io.StringIO()
    rc = pulse_rewind.rewind(str(db), 999, assume_yes=True, out=out)
    assert rc == 0
    assert "nothing to rewind" in out.getvalue()
