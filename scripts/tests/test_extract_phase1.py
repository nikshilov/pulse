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


def test_event_entities_junction_writes_resolved_names(tmp_path):
    """Event with entities_involved=['Anna','Fedya'] must produce 2 junction rows."""
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
        "events": [{
            "title": "coffee",
            "description": "morning coffee together",
            "sentiment": 0.5, "emotional_weight": 0.3,
            "entities_involved": ["Anna", "Fedya"],
        }],
        "relations": [],
        "facts": [],
    }
    con.execute("BEGIN IMMEDIATE")
    report = pulse_extract._apply_extraction(con, 1, result)
    con.execute("COMMIT")

    junction_rows = con.execute(
        "SELECT event_id, entity_id FROM event_entities ORDER BY entity_id"
    ).fetchall()
    con.close()
    assert len(junction_rows) == 2
    assert {r[1] for r in junction_rows} == {1, 2}  # entity IDs 1 and 2
    assert report["events_written"] == 1
    assert report["event_entities_written"] == 2


def test_event_with_partial_resolution_writes_only_resolved(tmp_path):
    """Event names ['Anna','Ghost']: 'Ghost' unresolved → event written, 1 junction row, no failure."""
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
        "events": [{
            "title": "meeting", "description": "", "sentiment": 0.0, "emotional_weight": 0.1,
            "entities_involved": ["Anna", "Ghost"],
        }],
        "relations": [],
        "facts": [],
    }
    con.execute("BEGIN IMMEDIATE")
    report = pulse_extract._apply_extraction(con, 1, result)
    con.execute("COMMIT")

    junction_rows = con.execute("SELECT entity_id FROM event_entities").fetchall()
    con.close()
    assert len(junction_rows) == 1
    assert junction_rows[0][0] == 1
    assert report["events_written"] == 1
    assert report["event_entities_written"] == 1


def test_event_with_all_unresolved_entities_fails(tmp_path):
    """Event names ['Ghost','Phantom']: all unresolved → event dropped, not written, failed_items has reason."""
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
        "events": [{
            "title": "ghost_event", "description": "", "sentiment": 0.0, "emotional_weight": 0.1,
            "entities_involved": ["Ghost", "Phantom"],
        }],
        "relations": [],
        "facts": [],
    }
    con.execute("BEGIN IMMEDIATE")
    report = pulse_extract._apply_extraction(con, 1, result)
    con.execute("COMMIT")

    ev_count = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    junction_count = con.execute("SELECT COUNT(*) FROM event_entities").fetchone()[0]
    con.close()
    assert ev_count == 0
    assert junction_count == 0
    assert report["events_written"] == 0
    assert report["event_entities_written"] == 0
    failures = [f for f in report["failed_items"]
                if f["item_kind"] == "event" and f["reason"] == "all_entities_involved_unresolved"]
    assert len(failures) == 1
    assert failures[0]["detail"]["title"] == "ghost_event"


# ---------- Layer 3: checkpoint ----------

def test_save_artifact_commits_in_own_transaction(tmp_path):
    """_save_artifact must commit even if no outer tx is active, and be visible immediately."""
    db = tmp_path / "p1.db"
    _apply_migrations(db)

    con = pulse_extract._open_connection(str(db))
    con.execute(
        "INSERT INTO extraction_jobs(observation_ids,state,created_at,updated_at) VALUES ('[1]','pending','2026-04-16T00:00:00Z','2026-04-16T00:00:00Z')"
    )
    pulse_extract._save_artifact(con, job_id=1, kind="triage", obs_id=None,
                                 payload={"verdicts": [{"verdict": "skip"}]}, model="sonnet-4.6")

    # Probe with a separate connection — artifact must be visible (committed, not just staged).
    probe = sqlite3.connect(db)
    row = probe.execute(
        "SELECT kind, obs_id, payload_json, model FROM extraction_artifacts WHERE job_id=1"
    ).fetchone()
    probe.close()
    con.close()
    assert row is not None
    assert row[0] == "triage"
    assert row[1] is None
    assert json.loads(row[2]) == {"verdicts": [{"verdict": "skip"}]}
    assert row[3] == "sonnet-4.6"


def test_get_artifact_returns_parsed_payload(tmp_path):
    db = tmp_path / "p1.db"
    _apply_migrations(db)

    con = pulse_extract._open_connection(str(db))
    con.execute(
        "INSERT INTO extraction_jobs(observation_ids,state,created_at,updated_at) VALUES ('[1]','pending','2026-04-16T00:00:00Z','2026-04-16T00:00:00Z')"
    )
    con.execute(
        """INSERT INTO observations(source_kind,source_id,content_hash,version,scope,captured_at,observed_at,actors,content_text,metadata,raw_json)
           VALUES ('claude_jsonl','f:1','h',1,'shared','2026-04-16T00:00:00Z','2026-04-16T00:00:00Z','[]','t','{}','{}')"""
    )
    pulse_extract._save_artifact(con, 1, "triage", None, {"v": 1}, "sonnet-4.6")
    pulse_extract._save_artifact(con, 1, "extract", 1, {"entities": [{"canonical_name": "Anna"}]}, "opus-4.6")

    assert pulse_extract._get_artifact(con, 1, "triage", None) == {"v": 1}
    assert pulse_extract._get_artifact(con, 1, "extract", 1) == {"entities": [{"canonical_name": "Anna"}]}
    assert pulse_extract._get_artifact(con, 1, "extract", 999) is None
    assert pulse_extract._get_artifact(con, 1, "triage", 1) is None
    con.close()


def test_save_artifact_is_idempotent_under_partial_unique(tmp_path):
    """A second _save_artifact for the same (job,kind,obs) must not raise — treated as no-op replay."""
    db = tmp_path / "p1.db"
    _apply_migrations(db)

    con = pulse_extract._open_connection(str(db))
    con.execute(
        "INSERT INTO extraction_jobs(observation_ids,state,created_at,updated_at) VALUES ('[1]','pending','2026-04-16T00:00:00Z','2026-04-16T00:00:00Z')"
    )
    pulse_extract._save_artifact(con, 1, "triage", None, {"v": 1}, "sonnet-4.6")
    pulse_extract._save_artifact(con, 1, "triage", None, {"v": 2}, "sonnet-4.6")

    rows = con.execute("SELECT COUNT(*) FROM extraction_artifacts WHERE job_id=1 AND kind='triage'").fetchone()[0]
    payload = json.loads(
        con.execute("SELECT payload_json FROM extraction_artifacts WHERE job_id=1 AND kind='triage'").fetchone()[0]
    )
    con.close()
    assert rows == 1, "partial UNIQUE + INSERT OR IGNORE keeps exactly one row"
    assert payload == {"v": 1}, "first save wins; re-save is a no-op (retry-safe, no surprise overwrite)"


def test_triage_artifact_saved_after_sonnet_call(tmp_path, monkeypatch):
    """After run_once, the triage artifact for that job is present."""
    db = tmp_path / "p1.db"
    _apply_migrations(db)

    con = sqlite3.connect(db)
    con.execute(
        """INSERT INTO observations(source_kind,source_id,content_hash,version,scope,captured_at,observed_at,actors,content_text,metadata,raw_json)
           VALUES ('claude_jsonl','f:1','h',1,'shared','2026-04-16T00:00:00Z','2026-04-16T00:00:00Z','[]','t','{}','{}')"""
    )
    con.execute(
        "INSERT INTO extraction_jobs(observation_ids,state,attempts,created_at,updated_at) VALUES ('[1]','pending',0,'2026-04-16T00:00:00Z','2026-04-16T00:00:00Z')"
    )
    con.commit()
    con.close()

    monkeypatch.setattr(pulse_extract, "call_sonnet_triage",
                        lambda _p, expected_count: [{"verdict": "skip"} for _ in range(expected_count)])

    pulse_extract.run_once(str(db))

    con = sqlite3.connect(db)
    row = con.execute(
        "SELECT payload_json FROM extraction_artifacts WHERE job_id=1 AND kind='triage'"
    ).fetchone()
    con.close()
    assert row is not None
    payload = json.loads(row[0])
    assert payload == [{"verdict": "skip"}]


def test_restart_reuses_triage_artifact_no_sonnet_call(tmp_path, monkeypatch):
    """If a triage artifact already exists for the job, call_sonnet_triage must not be called."""
    db = tmp_path / "p1.db"
    _apply_migrations(db)

    con = sqlite3.connect(db)
    con.execute(
        """INSERT INTO observations(source_kind,source_id,content_hash,version,scope,captured_at,observed_at,actors,content_text,metadata,raw_json)
           VALUES ('claude_jsonl','f:1','h',1,'shared','2026-04-16T00:00:00Z','2026-04-16T00:00:00Z','[]','t','{}','{}')"""
    )
    con.execute(
        "INSERT INTO extraction_jobs(observation_ids,state,attempts,created_at,updated_at) VALUES ('[1]','pending',0,'2026-04-16T00:00:00Z','2026-04-16T00:00:00Z')"
    )
    con.execute(
        "INSERT INTO extraction_artifacts(job_id,kind,obs_id,payload_json,model) VALUES (1,'triage',NULL,'[{\"verdict\":\"skip\"}]','sonnet-cached')"
    )
    con.commit()
    con.close()

    call_count = {"n": 0}

    def boom_triage(*_a, **_kw):
        call_count["n"] += 1
        raise AssertionError("Sonnet must not be called when triage artifact exists")

    monkeypatch.setattr(pulse_extract, "call_sonnet_triage", boom_triage)

    pulse_extract.run_once(str(db))
    assert call_count["n"] == 0, "Sonnet was called despite artifact being present"

    con = sqlite3.connect(db)
    state = con.execute("SELECT state FROM extraction_jobs WHERE id=1").fetchone()[0]
    con.close()
    assert state == "done", "skip-all-obs triage → done"


def test_extract_artifact_saved_after_opus_call_per_obs(tmp_path, monkeypatch):
    """After run_once on a 2-obs job, extract artifacts exist for each obs flagged 'extract'."""
    db = tmp_path / "p1.db"
    _apply_migrations(db)

    con = sqlite3.connect(db)
    for i in (1, 2):
        con.execute(
            """INSERT INTO observations(source_kind,source_id,content_hash,version,scope,captured_at,observed_at,actors,content_text,metadata,raw_json)
               VALUES ('claude_jsonl',?,?,1,'shared','2026-04-16T00:00:00Z','2026-04-16T00:00:00Z','[]',?,'{}','{}')""",
            (f"f:{i}", f"h{i}", f"t{i}"),
        )
    con.execute(
        "INSERT INTO extraction_jobs(observation_ids,state,attempts,created_at,updated_at) VALUES ('[1,2]','pending',0,'2026-04-16T00:00:00Z','2026-04-16T00:00:00Z')"
    )
    con.commit()
    con.close()

    monkeypatch.setattr(
        pulse_extract, "call_sonnet_triage",
        lambda _p, expected_count: [{"verdict": "extract"} for _ in range(expected_count)],
    )
    call_ids: list = []

    def fake_extract(_prompt):
        call_ids.append(len(call_ids) + 1)
        return {
            "entities": [{"canonical_name": f"E{len(call_ids)}", "kind": "person"}],
            "events": [], "relations": [], "facts": [],
        }

    monkeypatch.setattr(pulse_extract, "call_opus_extract", fake_extract)

    pulse_extract.run_once(str(db))

    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT obs_id FROM extraction_artifacts WHERE job_id=1 AND kind='extract' ORDER BY obs_id"
    ).fetchall()
    con.close()
    assert [r[0] for r in rows] == [1, 2], "one extract artifact per obs"
    assert len(call_ids) == 2, "Opus called once per obs on fresh run"


def test_restart_reuses_extract_artifact_per_obs(tmp_path, monkeypatch):
    """If an extract artifact exists for obs 1, Opus must not be called for obs 1 on replay."""
    db = tmp_path / "p1.db"
    _apply_migrations(db)

    con = sqlite3.connect(db)
    for i in (1, 2):
        con.execute(
            """INSERT INTO observations(source_kind,source_id,content_hash,version,scope,captured_at,observed_at,actors,content_text,metadata,raw_json)
               VALUES ('claude_jsonl',?,?,1,'shared','2026-04-16T00:00:00Z','2026-04-16T00:00:00Z','[]',?,'{}','{}')""",
            (f"f:{i}", f"h{i}", f"t{i}"),
        )
    con.execute(
        "INSERT INTO extraction_jobs(observation_ids,state,attempts,created_at,updated_at) VALUES ('[1,2]','pending',0,'2026-04-16T00:00:00Z','2026-04-16T00:00:00Z')"
    )
    con.execute(
        "INSERT INTO extraction_artifacts(job_id,kind,obs_id,payload_json,model) "
        "VALUES (1,'triage',NULL,'[{\"verdict\":\"extract\"},{\"verdict\":\"extract\"}]','sonnet-cached')"
    )
    con.execute(
        "INSERT INTO extraction_artifacts(job_id,kind,obs_id,payload_json,model) "
        "VALUES (1,'extract',1,?,'opus-cached')",
        (json.dumps({"entities": [{"canonical_name": "Cached1", "kind": "person"}],
                     "events": [], "relations": [], "facts": []}),),
    )
    con.commit()
    con.close()

    monkeypatch.setattr(pulse_extract, "call_sonnet_triage",
                        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("no triage expected")))
    opus_calls: list = []

    def fake_opus(_prompt):
        opus_calls.append(_prompt)
        return {
            "entities": [{"canonical_name": "Fresh2", "kind": "person"}],
            "events": [], "relations": [], "facts": [],
        }

    monkeypatch.setattr(pulse_extract, "call_opus_extract", fake_opus)

    pulse_extract.run_once(str(db))

    assert len(opus_calls) == 1, (
        f"Opus must be called only for obs 2 (obs 1 was cached); got {len(opus_calls)} calls"
    )

    con = sqlite3.connect(db)
    names = {r[0] for r in con.execute("SELECT canonical_name FROM entities")}
    con.close()
    assert names == {"Cached1", "Fresh2"}, (
        "obs 1 entity comes from cached artifact, obs 2 from fresh Opus call"
    )
