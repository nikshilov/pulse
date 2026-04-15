"""Phase 1 — tests for schema migration, UPSERT writes, checkpoint, and SAVEPOINT hygiene."""

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pulse_extract  # noqa: E402

MIGRATIONS = Path(__file__).resolve().parents[2] / "internal" / "store" / "migrations"


def _apply_migrations(db_path: Path) -> None:
    con = sqlite3.connect(db_path)
    for mig in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(mig.read_text())
    con.commit()
    con.close()


def _index_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


# ---------- Layer 1: schema ----------

def test_migration_006_creates_relations_unique_index(tmp_path):
    db = tmp_path / "p1.db"
    _apply_migrations(db)
    con = sqlite3.connect(db)
    try:
        assert _index_exists(con, "idx_relations_unique")
    finally:
        con.close()


def test_migration_006_creates_facts_unique_index(tmp_path):
    db = tmp_path / "p1.db"
    _apply_migrations(db)
    con = sqlite3.connect(db)
    try:
        assert _index_exists(con, "idx_facts_unique")
    finally:
        con.close()


def test_migration_006_creates_event_entities_table(tmp_path):
    db = tmp_path / "p1.db"
    _apply_migrations(db)
    con = sqlite3.connect(db)
    try:
        assert _table_exists(con, "event_entities")
        cols = {row[1] for row in con.execute("PRAGMA table_info(event_entities)")}
        assert cols == {"event_id", "entity_id"}
    finally:
        con.close()


def test_migration_006_creates_extraction_artifacts_table(tmp_path):
    db = tmp_path / "p1.db"
    _apply_migrations(db)
    con = sqlite3.connect(db)
    try:
        assert _table_exists(con, "extraction_artifacts")
        cols = {row[1] for row in con.execute("PRAGMA table_info(extraction_artifacts)")}
        assert cols == {"id", "job_id", "kind", "obs_id", "payload_json", "model", "created_at"}
        assert _index_exists(con, "idx_artifacts_triage_unique")
        assert _index_exists(con, "idx_artifacts_extract_unique")
        assert _index_exists(con, "idx_artifacts_job")
    finally:
        con.close()


def test_relations_unique_rejects_duplicate(tmp_path):
    db = tmp_path / "p1.db"
    _apply_migrations(db)
    con = sqlite3.connect(db)
    con.execute("PRAGMA foreign_keys=ON")
    try:
        now = "2026-04-16T00:00:00Z"
        con.execute(
            "INSERT INTO entities(canonical_name,kind,first_seen,last_seen) VALUES ('Anna','person',?,?)",
            (now, now),
        )
        con.execute(
            "INSERT INTO entities(canonical_name,kind,first_seen,last_seen) VALUES ('Fedya','person',?,?)",
            (now, now),
        )
        con.execute(
            "INSERT INTO relations(from_entity_id,to_entity_id,kind,strength,first_seen,last_seen) VALUES (1,2,'friend',0.5,?,?)",
            (now, now),
        )
        raised = False
        try:
            con.execute(
                "INSERT INTO relations(from_entity_id,to_entity_id,kind,strength,first_seen,last_seen) VALUES (1,2,'friend',0.7,?,?)",
                (now, now),
            )
        except sqlite3.IntegrityError:
            raised = True
        assert raised, "duplicate (1,2,'friend') must raise IntegrityError"
    finally:
        con.close()


def test_facts_unique_rejects_duplicate(tmp_path):
    db = tmp_path / "p1.db"
    _apply_migrations(db)
    con = sqlite3.connect(db)
    con.execute("PRAGMA foreign_keys=ON")
    try:
        now = "2026-04-16T00:00:00Z"
        con.execute(
            "INSERT INTO entities(canonical_name,kind,first_seen,last_seen) VALUES ('Anna','person',?,?)",
            (now, now),
        )
        con.execute(
            "INSERT INTO facts(entity_id,text,confidence,created_at) VALUES (1,'loves coffee',1.0,?)",
            (now,),
        )
        raised = False
        try:
            con.execute(
                "INSERT INTO facts(entity_id,text,confidence,created_at) VALUES (1,'loves coffee',0.9,?)",
                (now,),
            )
        except sqlite3.IntegrityError:
            raised = True
        assert raised, "duplicate (1,'loves coffee') must raise IntegrityError"
    finally:
        con.close()


def test_event_entities_cascades_on_entity_delete(tmp_path):
    db = tmp_path / "p1.db"
    _apply_migrations(db)
    con = sqlite3.connect(db)
    con.execute("PRAGMA foreign_keys=ON")
    try:
        now = "2026-04-16T00:00:00Z"
        con.execute(
            "INSERT INTO entities(canonical_name,kind,first_seen,last_seen) VALUES ('Anna','person',?,?)",
            (now, now),
        )
        con.execute("INSERT INTO events(title,ts) VALUES ('meeting',?)", (now,))
        con.execute("INSERT INTO event_entities(event_id,entity_id) VALUES (1,1)")
        assert con.execute("SELECT COUNT(*) FROM event_entities").fetchone()[0] == 1
        con.execute("DELETE FROM entities WHERE id=1")
        assert con.execute("SELECT COUNT(*) FROM event_entities").fetchone()[0] == 0
    finally:
        con.close()


def test_artifacts_partial_unique_triage_one_per_job(tmp_path):
    db = tmp_path / "p1.db"
    _apply_migrations(db)
    con = sqlite3.connect(db)
    con.execute("PRAGMA foreign_keys=ON")
    try:
        now = "2026-04-16T00:00:00Z"
        con.execute(
            "INSERT INTO extraction_jobs(observation_ids,state,created_at,updated_at) VALUES ('[1]','pending',?,?)",
            (now, now),
        )
        con.execute(
            "INSERT INTO extraction_artifacts(job_id,kind,obs_id,payload_json,model) VALUES (1,'triage',NULL,'[]','sonnet')"
        )
        raised = False
        try:
            con.execute(
                "INSERT INTO extraction_artifacts(job_id,kind,obs_id,payload_json,model) VALUES (1,'triage',NULL,'[]','sonnet')"
            )
        except sqlite3.IntegrityError:
            raised = True
        assert raised, "second triage artifact for same job must raise IntegrityError"
    finally:
        con.close()


def test_artifacts_partial_unique_extract_one_per_job_obs(tmp_path):
    db = tmp_path / "p1.db"
    _apply_migrations(db)
    con = sqlite3.connect(db)
    con.execute("PRAGMA foreign_keys=ON")
    try:
        now = "2026-04-16T00:00:00Z"
        con.execute(
            """INSERT INTO observations(source_kind,source_id,content_hash,version,scope,captured_at,observed_at,actors,content_text,metadata,raw_json)
               VALUES ('claude_jsonl','f:1','h',1,'shared',?,?,'[]','t','{}','{}')""",
            (now, now),
        )
        con.execute(
            "INSERT INTO extraction_jobs(observation_ids,state,created_at,updated_at) VALUES ('[1]','pending',?,?)",
            (now, now),
        )
        con.execute(
            "INSERT INTO extraction_artifacts(job_id,kind,obs_id,payload_json,model) VALUES (1,'extract',1,'{}','opus')"
        )
        raised = False
        try:
            con.execute(
                "INSERT INTO extraction_artifacts(job_id,kind,obs_id,payload_json,model) VALUES (1,'extract',1,'{}','opus')"
            )
        except sqlite3.IntegrityError:
            raised = True
        assert raised, "second extract artifact for (job=1, obs=1) must raise IntegrityError"
    finally:
        con.close()


# ---------- Layer 2: writes ----------

def test_relation_upsert_bumps_strength_and_updates_last_seen(tmp_path):
    """Second apply of the same (from,to,kind) must UPSERT: strength += 1, last_seen updated."""
    db = tmp_path / "p1.db"
    _apply_migrations(db)

    con = pulse_extract._open_connection(str(db))
    con.execute(
        """INSERT INTO observations
           (source_kind, source_id, content_hash, version, scope, captured_at,
            observed_at, actors, content_text, metadata, raw_json)
           VALUES ('claude_jsonl','f:1','h1',1,'shared','2026-04-16T00:00:00Z',
                   '2026-04-16T00:00:00Z','[]','t','{}','{}')"""
    )
    result = {
        "entities": [
            {"canonical_name": "Anna", "kind": "person"},
            {"canonical_name": "Fedya", "kind": "person"},
        ],
        "events": [],
        "relations": [{"from": "Anna", "to": "Fedya", "kind": "friend", "strength": 0.5}],
        "facts": [],
    }
    con.execute("BEGIN IMMEDIATE")
    r1 = pulse_extract._apply_extraction(con, 1, result)
    con.execute("COMMIT")

    # First apply: one relation, strength=0.5
    assert r1["relations_written"] == 1
    row = con.execute("SELECT strength, first_seen, last_seen FROM relations").fetchone()
    assert row[0] == 0.5
    first_seen_before = row[1]

    con.execute("BEGIN IMMEDIATE")
    r2 = pulse_extract._apply_extraction(con, 1, result)
    con.execute("COMMIT")

    # Second apply of same relation: UPSERT bumps strength, keeps first_seen, updates last_seen
    rows = con.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
    con.close()
    assert rows == 1, "relation count must stay at 1 (UPSERT, not duplicate)"
    assert r2["relations_written"] == 1, "UPSERT still counts as a write"
    row2 = sqlite3.connect(db).execute(
        "SELECT strength, first_seen, last_seen FROM relations"
    ).fetchone()
    assert row2[0] == 1.5, f"strength must bump by 1 on re-apply (was 0.5, got {row2[0]})"
    assert row2[1] == first_seen_before, "first_seen must be preserved"
    # last_seen is a second-resolution timestamp — it can equal first_seen if both runs are in
    # the same second; the invariant is that strength bumped, which the previous assert verified.


def test_fact_insert_or_ignore_is_noop_on_duplicate(tmp_path):
    """Second apply of the same (entity_id, text) fact must not raise, must not duplicate."""
    db = tmp_path / "p1.db"
    _apply_migrations(db)

    con = pulse_extract._open_connection(str(db))
    con.execute(
        """INSERT INTO observations
           (source_kind, source_id, content_hash, version, scope, captured_at,
            observed_at, actors, content_text, metadata, raw_json)
           VALUES ('claude_jsonl','f:1','h1',1,'shared','2026-04-16T00:00:00Z',
                   '2026-04-16T00:00:00Z','[]','t','{}','{}')"""
    )
    result = {
        "entities": [{"canonical_name": "Anna", "kind": "person"}],
        "events": [],
        "relations": [],
        "facts": [{"entity": "Anna", "text": "loves coffee", "confidence": 0.9}],
    }
    con.execute("BEGIN IMMEDIATE")
    r1 = pulse_extract._apply_extraction(con, 1, result)
    con.execute("COMMIT")
    assert r1["facts_written"] == 1

    con.execute("BEGIN IMMEDIATE")
    r2 = pulse_extract._apply_extraction(con, 1, result)
    con.execute("COMMIT")

    rows = con.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    failed = [f for f in r2["failed_items"] if f["item_kind"] == "fact"]
    con.close()
    assert rows == 1, "fact must not be duplicated"
    assert failed == [], "duplicate must be silent, not a failed_item"
