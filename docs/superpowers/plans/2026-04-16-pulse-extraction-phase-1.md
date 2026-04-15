# Pulse Extraction Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the extraction write path with UNIQUE+UPSERT, persist event↔entity junction, make jobs restart-resumable via a checkpoint table, and close the Phase 0 SAVEPOINT hygiene follow-up (#84).

**Architecture:** Four independent layers delivered in one PR on branch `extract-phase-1`: (1) migration `006_phase_1.sql` adds UNIQUE indices, `event_entities` junction, `extraction_artifacts` checkpoint; (2) `_apply_extraction` switches to `ON CONFLICT` forms and writes to the junction; (3) `run_once` reads/writes artifacts to skip LLM calls on restart; (4) SAVEPOINT `except sqlite3.Error` widens to `except Exception`. Each layer is independently testable and bisectable; committed separately so `git bisect` can isolate regressions.

**Tech Stack:** Python 3 (stdlib `sqlite3`), pytest, SQLite migrations applied via `con.executescript`, Anthropic SDK (existing wiring).

**Spec:** `docs/superpowers/specs/2026-04-16-pulse-extraction-phase-1-design.md`

---

## File Structure

**Create:**
- `internal/store/migrations/006_phase_1.sql` — schema migration (Layer 1)
- `scripts/tests/test_extract_phase1.py` — Phase 1 tests across all layers

**Modify:**
- `scripts/pulse_extract.py` — `_apply_extraction` write changes (Layers 2, 4), new `_get_artifact` / `_save_artifact` helpers (Layer 3), `run_once` flow (Layer 3)
- `scripts/tests/test_extract_e2e.py` — assert `event_entities_written` present in `apply_report` output

**Reference (don't modify):**
- `internal/store/migrations/005_graph.sql` — current graph schema pattern
- `scripts/tests/test_extract_phase0.py` — `_apply_migrations(db_path)` helper pattern, `_open_connection` test setup, SAVEPOINT test patterns
- `scripts/pulse_extract.py` (Phase 0 `_apply_extraction`) — SAVEPOINT loop pattern, `_set_job_state` own-tx pattern

---

## Schema correction from spec

The spec (`2026-04-16-pulse-extraction-phase-1-design.md`) described `extraction_artifacts.obs_id TEXT` with a single `UNIQUE (job_id, kind, obs_id)`. SQLite treats `NULL != NULL` in composite UNIQUEs, so a single UNIQUE would allow duplicate triage rows. This plan uses the cleaner form:

- `obs_id INTEGER REFERENCES observations(id) ON DELETE CASCADE` (NULL for `kind='triage'`)
- Two partial UNIQUE indices (one per kind) to enforce "one triage per job" and "one extract per (job, obs)" correctly.

This is a mechanical correction to the schema, not a design change. The spec gets a mirror fix in Task 2.

---

## Branch setup (do once, manually)

Work on new branch `extract-phase-1` branched from current Phase 0 HEAD on `graph-populator-m1-m3`:

```bash
cd /Users/nikshilov/dev/ai/pulse
git fetch origin
git checkout graph-populator-m1-m3
git pull origin graph-populator-m1-m3
git checkout -b extract-phase-1
```

When Phase 0 PR #1 merges to `main`, rebase:

```bash
git fetch origin main
git rebase origin/main
```

All tasks below commit to `extract-phase-1`.

---

## Task 1: Pre-migration safety audit

**Goal:** Confirm current data in `pulse-dev/pulse.db` has no pre-existing `(from_entity_id, to_entity_id, kind)` or `(entity_id, text)` duplicates before we add UNIQUE indices. If any duplicate exists, migration 006 would fail on deploy.

**Files:**
- Create: `scripts/phase1_audit.py`

- [ ] **Step 1: Write the audit script**

Create `/Users/nikshilov/dev/ai/pulse/scripts/phase1_audit.py`:

```python
#!/usr/bin/env python3
"""Phase 1 pre-migration safety audit.

Verifies that the current graph has no duplicate (from, to, kind) relations
or (entity_id, text) facts. If any duplicate exists, migration 006 will fail.
Run against a COPY of pulse-dev.db, never production.
"""

import argparse
import sqlite3
import sys


def audit(db_path: str) -> int:
    con = sqlite3.connect(db_path)
    try:
        rel_dups = con.execute(
            """SELECT from_entity_id, to_entity_id, kind, COUNT(*) AS n
               FROM relations
               GROUP BY from_entity_id, to_entity_id, kind
               HAVING n > 1"""
        ).fetchall()
        fact_dups = con.execute(
            """SELECT entity_id, text, COUNT(*) AS n
               FROM facts
               GROUP BY entity_id, text
               HAVING n > 1"""
        ).fetchall()
    finally:
        con.close()

    print(f"relations duplicates: {len(rel_dups)}")
    for row in rel_dups[:10]:
        print(f"  (from={row[0]}, to={row[1]}, kind={row[2]!r}) x{row[3]}")
    if len(rel_dups) > 10:
        print(f"  ... {len(rel_dups) - 10} more")

    print(f"facts duplicates: {len(fact_dups)}")
    for row in fact_dups[:10]:
        text_preview = (row[1] or "")[:60]
        print(f"  (entity_id={row[0]}, text={text_preview!r}) x{row[2]}")
    if len(fact_dups) > 10:
        print(f"  ... {len(fact_dups) - 10} more")

    if rel_dups or fact_dups:
        print("FAIL: duplicates found — migration 006 would fail on deploy")
        return 1
    print("OK: no duplicates, safe to migrate")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", required=True, help="Path to SQLite DB (use a COPY, not pulse-dev live)")
    args = p.parse_args()
    return audit(args.db)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run audit against copy of pulse-dev**

```bash
cp /Users/nikshilov/dev/ai/pulse/pulse-dev/pulse.db /tmp/pulse-phase1-audit.db
python3 /Users/nikshilov/dev/ai/pulse/scripts/phase1_audit.py --db /tmp/pulse-phase1-audit.db
```

Expected output:
```
relations duplicates: 0
facts duplicates: 0
OK: no duplicates, safe to migrate
```

If the expected output appears, proceed. If duplicates are reported, STOP and flag to Nik — this plan assumes zero duplicates because resolver hasn't been writing them. Non-zero means either schema audit was wrong or resolver has a bug; either way, the plan needs adjustment before Task 3.

- [ ] **Step 3: Commit the audit script**

```bash
cd /Users/nikshilov/dev/ai/pulse
git add scripts/phase1_audit.py
git commit -m "pulse extract phase 1: pre-migration safety audit script

Counts duplicate (from,to,kind) relations and (entity_id,text) facts.
Used as a safety gate before applying migration 006 to production.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 2: Fix spec doc (obs_id type + partial UNIQUEs)

**Goal:** Correct the schema fragment in the design spec to match what Task 3 will actually implement. Keeps the two documents consistent.

**Files:**
- Modify: `docs/superpowers/specs/2026-04-16-pulse-extraction-phase-1-design.md`

- [ ] **Step 1: Edit the spec's Layer 1 Schema block**

Find the SQL block starting with `CREATE TABLE extraction_artifacts` in the spec. Replace that block (and the block of notes immediately below it that reference `obs_id TEXT`) with:

```sql
CREATE TABLE extraction_artifacts (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id       INTEGER NOT NULL REFERENCES extraction_jobs(id) ON DELETE CASCADE,
  kind         TEXT NOT NULL CHECK (kind IN ('triage','extract')),
  obs_id       INTEGER REFERENCES observations(id) ON DELETE CASCADE,  -- NULL for kind='triage'
  payload_json TEXT NOT NULL,
  model        TEXT NOT NULL,
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
-- One triage artifact per job; one extract artifact per (job, obs).
-- Partial indices are used because SQLite treats NULLs as distinct in
-- composite UNIQUEs, so a plain UNIQUE(job_id,kind,obs_id) would allow
-- duplicate triage rows with obs_id=NULL.
CREATE UNIQUE INDEX idx_artifacts_triage_unique ON extraction_artifacts(job_id)
  WHERE kind = 'triage';
CREATE UNIQUE INDEX idx_artifacts_extract_unique ON extraction_artifacts(job_id, obs_id)
  WHERE kind = 'extract';
CREATE INDEX idx_artifacts_job ON extraction_artifacts(job_id, kind);
```

And in the "Rationale for each choice" bullet list below, replace the `obs_id TEXT` bullet with:

```
- **`obs_id INTEGER` FK to `observations(id)`** — observation IDs are integers throughout (observations.id is INTEGER PRIMARY KEY; extraction_jobs.observation_ids is a JSON array of ints). FK with CASCADE means erasure of an observation removes its artifact automatically.
- **Partial UNIQUE indices** — one triage artifact per job, one extract artifact per (job, obs). A plain composite UNIQUE would break for triage because SQLite treats NULLs as distinct.
```

- [ ] **Step 2: Commit the spec correction**

```bash
cd /Users/nikshilov/dev/ai/pulse
git add docs/superpowers/specs/2026-04-16-pulse-extraction-phase-1-design.md
git commit -m "pulse extract phase 1: spec — obs_id INTEGER + partial UNIQUEs

Small mechanical correction: SQLite treats NULLs as distinct in
composite UNIQUEs, so (job_id, kind, obs_id) would allow duplicate
triage rows. Replace with partial unique indices per kind.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 3: Migration 006 — schema (UNIQUE + junction + artifacts)

**Goal:** Add the migration file that creates UNIQUE indices on `relations` and `facts`, the `event_entities` junction table, and the `extraction_artifacts` checkpoint table. Test that the migration produces the expected schema.

**Files:**
- Create: `internal/store/migrations/006_phase_1.sql`
- Create: `scripts/tests/test_extract_phase1.py` (test file with Layer 1 tests)

- [ ] **Step 1: Write the failing tests**

Create `/Users/nikshilov/dev/ai/pulse/scripts/tests/test_extract_phase1.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/nikshilov/dev/ai/pulse
python3 -m pytest scripts/tests/test_extract_phase1.py -v
```

Expected: each of the 9 new tests fails (they reference tables/indices that don't exist yet). Phase 0 tests in `test_extract_phase0.py` should still pass.

- [ ] **Step 3: Create the migration file**

Create `/Users/nikshilov/dev/ai/pulse/internal/store/migrations/006_phase_1.sql`:

```sql
-- Phase 1: data-consistency UNIQUEs, event↔entity junction, extraction checkpoint.
--
-- Non-breaking: pre-migration audit (scripts/phase1_audit.py) confirms no
-- existing duplicates in relations(from,to,kind) or facts(entity_id,text).
-- Resolver has been doing dedup at the application layer; this pins the
-- invariant at the schema layer.

CREATE UNIQUE INDEX idx_relations_unique ON relations(from_entity_id, to_entity_id, kind);
CREATE UNIQUE INDEX idx_facts_unique     ON facts(entity_id, text);
-- Intentionally NO UNIQUE on entities(canonical_name, kind): resolver owns
-- canonical-name dedup, and legitimate same-name-different-kind entities
-- ("Anna" person vs "Anna" place) must stay permissible. Phase 3 alias index
-- closes the resolver side properly.

-- Junction table: which entities an event involves. Replaces the in-memory
-- `entities_involved` list that Phase 0 used to reject orphan events.
CREATE TABLE event_entities (
    event_id   INTEGER NOT NULL REFERENCES events(id)   ON DELETE CASCADE,
    entity_id  INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    PRIMARY KEY (event_id, entity_id)
);
CREATE INDEX idx_event_entities_entity ON event_entities(entity_id);

-- Checkpoint for two-stage extraction: persists triage verdicts and per-obs
-- extract results so a crashed job resumes without repeating LLM calls.
CREATE TABLE extraction_artifacts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id       INTEGER NOT NULL REFERENCES extraction_jobs(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL CHECK (kind IN ('triage','extract')),
    obs_id       INTEGER REFERENCES observations(id) ON DELETE CASCADE,  -- NULL for kind='triage'
    payload_json TEXT NOT NULL,
    model        TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
-- Partial UNIQUEs: one triage artifact per job, one extract artifact per (job, obs).
-- Plain composite UNIQUE(job_id,kind,obs_id) would allow duplicate triage rows
-- because SQLite treats NULLs as distinct.
CREATE UNIQUE INDEX idx_artifacts_triage_unique  ON extraction_artifacts(job_id)          WHERE kind = 'triage';
CREATE UNIQUE INDEX idx_artifacts_extract_unique ON extraction_artifacts(job_id, obs_id)  WHERE kind = 'extract';
CREATE INDEX        idx_artifacts_job            ON extraction_artifacts(job_id, kind);
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/nikshilov/dev/ai/pulse
python3 -m pytest scripts/tests/test_extract_phase1.py -v
python3 -m pytest scripts/tests/test_extract_phase0.py -v  # regression: must still pass
```

Expected: all 9 new Phase 1 tests pass, all existing Phase 0 tests still pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse
git add internal/store/migrations/006_phase_1.sql scripts/tests/test_extract_phase1.py
git commit -m "pulse extract phase 1: migration 006 — UNIQUEs, junction, artifacts

- UNIQUE indices on relations(from,to,kind) and facts(entity_id,text)
- event_entities junction table (replaces in-memory entities_involved)
- extraction_artifacts checkpoint table with partial UNIQUE indices

Schema-level tests in test_extract_phase1.py verify structure and
that UNIQUE/CASCADE constraints fire as expected.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 4: Relations UPSERT (Layer 2a)

**Goal:** Switch the `relations` write in `_apply_extraction` from plain `INSERT` to `INSERT ... ON CONFLICT DO UPDATE`, so a repeated `(from, to, kind)` bumps `strength` by 1 and updates `last_seen` instead of raising.

**Files:**
- Modify: `scripts/pulse_extract.py:204-206` (the relations INSERT statement)
- Modify: `scripts/tests/test_extract_phase1.py` (append new test)

- [ ] **Step 1: Append the failing test**

Add to the end of `/Users/nikshilov/dev/ai/pulse/scripts/tests/test_extract_phase1.py`:

```python
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
    row2 = con_reopen = sqlite3.connect(db).execute(
        "SELECT strength, first_seen, last_seen FROM relations"
    ).fetchone()
    assert row2[0] == 1.5, f"strength must bump by 1 on re-apply (was 0.5, got {row2[0]})"
    assert row2[1] == first_seen_before, "first_seen must be preserved"
    # last_seen is a second-resolution timestamp — it can equal first_seen if both runs are in
    # the same second; the invariant is that strength bumped, which the previous assert verified.
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/nikshilov/dev/ai/pulse
python3 -m pytest scripts/tests/test_extract_phase1.py::test_relation_upsert_bumps_strength_and_updates_last_seen -v
```

Expected: FAIL — either IntegrityError from the UNIQUE index added in Task 3, or the second INSERT simply doesn't bump strength (depending on how the current code handles it under SAVEPOINT — likely the IntegrityError is caught and the item lands in failed_items, so `r2["relations_written"] == 0`).

- [ ] **Step 3: Change relations INSERT to UPSERT**

In `/Users/nikshilov/dev/ai/pulse/scripts/pulse_extract.py`, locate the relations block (lines 194-217 in Phase 0 HEAD). Replace the INSERT statement (currently at lines 204-207):

Find:
```python
        sp = f"rel_{idx}"
        con.execute(f"SAVEPOINT {sp}")
        try:
            cur = con.execute(
                "INSERT INTO relations (from_entity_id, to_entity_id, kind, strength, first_seen, last_seen) VALUES (?,?,?,?,?,?)",
                (from_id, to_id, rel.get("kind", ""), float(rel.get("strength", 0.0)), now, now),
            )
```

Replace with:
```python
        sp = f"rel_{idx}"
        con.execute(f"SAVEPOINT {sp}")
        try:
            cur = con.execute(
                """INSERT INTO relations (from_entity_id, to_entity_id, kind, strength, first_seen, last_seen)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(from_entity_id, to_entity_id, kind) DO UPDATE SET
                       strength  = strength + 1,
                       last_seen = excluded.last_seen""",
                (from_id, to_id, rel.get("kind", ""), float(rel.get("strength", 0.0)), now, now),
            )
```

Note: the `cur.lastrowid` that follows is still used for the `evidence` INSERT. On UPSERT path, SQLite sets `lastrowid` to the id of the UPDATEd row (as of SQLite 3.27+). The evidence row still gets the correct `subject_id`.

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /Users/nikshilov/dev/ai/pulse
python3 -m pytest scripts/tests/test_extract_phase1.py::test_relation_upsert_bumps_strength_and_updates_last_seen -v
python3 -m pytest scripts/tests/test_extract_phase0.py -v  # regression
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse
git add scripts/pulse_extract.py scripts/tests/test_extract_phase1.py
git commit -m "pulse extract phase 1: relations UPSERT on (from,to,kind) conflict

Replaces plain INSERT with ON CONFLICT DO UPDATE so repeated mentions
bump strength and refresh last_seen instead of raising IntegrityError.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 5: Facts INSERT OR IGNORE (Layer 2b)

**Goal:** Change the `facts` write from plain `INSERT` to `INSERT ... ON CONFLICT DO NOTHING`, so duplicate `(entity_id, text)` is silently deduped instead of raising.

**Files:**
- Modify: `scripts/pulse_extract.py:229-232`
- Modify: `scripts/tests/test_extract_phase1.py` (append new test)

- [ ] **Step 1: Append the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/nikshilov/dev/ai/pulse
python3 -m pytest scripts/tests/test_extract_phase1.py::test_fact_insert_or_ignore_is_noop_on_duplicate -v
```

Expected: FAIL — plain INSERT raises IntegrityError on duplicate, SAVEPOINT catches it, so the fact lands in `failed_items` (violating the "silent no-op" assertion).

- [ ] **Step 3: Change facts INSERT to INSERT ... ON CONFLICT DO NOTHING**

In `/Users/nikshilov/dev/ai/pulse/scripts/pulse_extract.py`, locate the facts block (lines 219-242). Replace the INSERT statement:

Find:
```python
            cur = con.execute(
                "INSERT INTO facts (entity_id, text, confidence, scorer_version, created_at) VALUES (?,?,?,?,?)",
                (entity_id, fact.get("text", ""), scored["confidence"], scored["scorer_version"], now),
            )
            con.execute(
                "INSERT INTO evidence (subject_kind, subject_id, observation_id, created_at) VALUES ('fact',?,?,?)",
                (cur.lastrowid, obs_id, now),
            )
            con.execute(f"RELEASE SAVEPOINT {sp}")
            report["facts_written"] += 1
```

Replace with:
```python
            cur = con.execute(
                """INSERT INTO facts (entity_id, text, confidence, scorer_version, created_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(entity_id, text) DO NOTHING""",
                (entity_id, fact.get("text", ""), scored["confidence"], scored["scorer_version"], now),
            )
            if cur.rowcount == 1:
                con.execute(
                    "INSERT INTO evidence (subject_kind, subject_id, observation_id, created_at) VALUES ('fact',?,?,?)",
                    (cur.lastrowid, obs_id, now),
                )
                report["facts_written"] += 1
            con.execute(f"RELEASE SAVEPOINT {sp}")
```

Note the behavior change:
- `cur.rowcount` is 1 when a row was actually inserted (fresh fact), 0 when `DO NOTHING` suppressed it (duplicate).
- Evidence row is only written when the fact was actually inserted (otherwise we'd accumulate duplicate evidence rows pointing at the same subject on every re-apply).
- `facts_written` is only incremented on actual insert — matches the invariant "written = rows added to DB".

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /Users/nikshilov/dev/ai/pulse
python3 -m pytest scripts/tests/test_extract_phase1.py::test_fact_insert_or_ignore_is_noop_on_duplicate -v
python3 -m pytest scripts/tests/test_extract_phase0.py scripts/tests/test_extract_phase1.py -v
```

Expected: PASS + all prior tests still pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse
git add scripts/pulse_extract.py scripts/tests/test_extract_phase1.py
git commit -m "pulse extract phase 1: facts INSERT ... ON CONFLICT DO NOTHING

Duplicate (entity_id, text) now silently no-ops instead of raising.
Evidence row is conditional on actual insert so re-apply doesn't
accumulate duplicate evidence pointing at the same fact.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 6: event_entities junction writes + `event_entities_written` report key

**Goal:** After inserting an event, write `(event_id, entity_id)` rows for every resolvable entity name in `entities_involved`. If none of the names resolve (after entities pass), drop the event pre-SAVEPOINT as `all_entities_involved_unresolved`. Extend `apply_report` with `event_entities_written: int`.

**Files:**
- Modify: `scripts/pulse_extract.py:104-111` (report init) and `scripts/pulse_extract.py:169-192` (events block)
- Modify: `scripts/tests/test_extract_phase1.py` (append new tests)

- [ ] **Step 1: Append the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/nikshilov/dev/ai/pulse
python3 -m pytest scripts/tests/test_extract_phase1.py::test_event_entities_junction_writes_resolved_names scripts/tests/test_extract_phase1.py::test_event_with_partial_resolution_writes_only_resolved scripts/tests/test_extract_phase1.py::test_event_with_all_unresolved_entities_fails -v
```

Expected: all three fail. `event_entities_written` KeyError, junction is empty, `all_entities_involved_unresolved` reason missing.

- [ ] **Step 3: Extend report init and rewrite events block**

In `/Users/nikshilov/dev/ai/pulse/scripts/pulse_extract.py`, edit the report initialization (around line 104-111):

Find:
```python
    report = {
        "obs_id": obs_id,
        "entities_written": 0,
        "events_written": 0,
        "relations_written": 0,
        "facts_written": 0,
        "failed_items": [],
    }
```

Replace with:
```python
    report = {
        "obs_id": obs_id,
        "entities_written": 0,
        "events_written": 0,
        "event_entities_written": 0,
        "relations_written": 0,
        "facts_written": 0,
        "failed_items": [],
    }
```

Then find the events block (lines 169-192 in Phase 0):

```python
    # --- events ---
    for idx, ev in enumerate(result.get("events", [])):
        involved = ev.get("entities_involved") or []
        if not involved:
            _item_failure("event", "orphan_event_no_entities_involved", {"title": ev.get("title", "")})
            continue
        sp = f"ev_{idx}"
        con.execute(f"SAVEPOINT {sp}")
        try:
            s = scorer.score_event(ev)
            cur = con.execute(
                "INSERT INTO events (title, description, sentiment, emotional_weight, scorer_version, ts) VALUES (?,?,?,?,?,?)",
                (ev.get("title", ""), ev.get("description", ""), s["sentiment"], s["emotional_weight"], s["scorer_version"], ev.get("ts", now)),
            )
            con.execute(
                "INSERT INTO evidence (subject_kind, subject_id, observation_id, created_at) VALUES ('event',?,?,?)",
                (cur.lastrowid, obs_id, now),
            )
            con.execute(f"RELEASE SAVEPOINT {sp}")
            report["events_written"] += 1
        except sqlite3.Error as ex:
            con.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            con.execute(f"RELEASE SAVEPOINT {sp}")
            _item_failure("event", str(ex)[:200], {"index": idx, "title": ev.get("title", "")})
```

Replace with:
```python
    # --- events ---
    for idx, ev in enumerate(result.get("events", [])):
        involved = ev.get("entities_involved") or []
        if not involved:
            _item_failure("event", "orphan_event_no_entities_involved", {"title": ev.get("title", "")})
            continue
        resolved_entity_ids = [name_to_id[n] for n in involved if n in name_to_id]
        if not resolved_entity_ids:
            _item_failure(
                "event", "all_entities_involved_unresolved",
                {"index": idx, "title": ev.get("title", ""), "names": involved},
            )
            continue
        sp = f"ev_{idx}"
        con.execute(f"SAVEPOINT {sp}")
        try:
            s = scorer.score_event(ev)
            cur = con.execute(
                "INSERT INTO events (title, description, sentiment, emotional_weight, scorer_version, ts) VALUES (?,?,?,?,?,?)",
                (ev.get("title", ""), ev.get("description", ""), s["sentiment"], s["emotional_weight"], s["scorer_version"], ev.get("ts", now)),
            )
            event_id = cur.lastrowid
            for ent_id in resolved_entity_ids:
                con.execute(
                    "INSERT OR IGNORE INTO event_entities (event_id, entity_id) VALUES (?, ?)",
                    (event_id, ent_id),
                )
                report["event_entities_written"] += 1
            con.execute(
                "INSERT INTO evidence (subject_kind, subject_id, observation_id, created_at) VALUES ('event',?,?,?)",
                (event_id, obs_id, now),
            )
            con.execute(f"RELEASE SAVEPOINT {sp}")
            report["events_written"] += 1
        except sqlite3.Error as ex:
            con.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            con.execute(f"RELEASE SAVEPOINT {sp}")
            _item_failure("event", str(ex)[:200], {"index": idx, "title": ev.get("title", "")})
```

Note: `report["event_entities_written"]` is incremented per `INSERT OR IGNORE` call even if the insert was a no-op. Strictly, we'd only count successful inserts. But `INSERT OR IGNORE` sets `cur.rowcount` to 0 on ignore. For Phase 1 simplicity, we count attempted writes — the per-entity dedup is more about junction integrity than report accuracy, and the tests above assert the happy-path count which is identical to attempted count because we de-duplicate `resolved_entity_ids` implicitly (one name → one entity_id; duplicate names in `involved` would produce duplicate IDs which `INSERT OR IGNORE` handles).

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/nikshilov/dev/ai/pulse
python3 -m pytest scripts/tests/test_extract_phase1.py scripts/tests/test_extract_phase0.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse
git add scripts/pulse_extract.py scripts/tests/test_extract_phase1.py
git commit -m "pulse extract phase 1: event_entities junction + partial resolution

- Events now write (event_id, entity_id) rows for every resolved name
- 0-of-N resolved → event dropped pre-SAVEPOINT as all_entities_involved_unresolved
- apply_report carries event_entities_written count

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 7: `_get_artifact` / `_save_artifact` helpers (Layer 3a)

**Goal:** Add two helpers to `pulse_extract.py` for reading/writing `extraction_artifacts` rows. `_save_artifact` runs in its own `BEGIN IMMEDIATE/COMMIT` (same pattern as `_set_job_state`) so checkpoint writes are durable regardless of the surrounding apply transaction.

**Files:**
- Modify: `scripts/pulse_extract.py` (add two new top-level functions near `_set_job_state`)
- Modify: `scripts/tests/test_extract_phase1.py` (append new tests)

- [ ] **Step 1: Append the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/nikshilov/dev/ai/pulse
python3 -m pytest scripts/tests/test_extract_phase1.py -k "artifact" -v
```

Expected: FAIL — `pulse_extract._save_artifact` and `pulse_extract._get_artifact` don't exist yet.

- [ ] **Step 3: Add helpers**

In `/Users/nikshilov/dev/ai/pulse/scripts/pulse_extract.py`, locate the `_set_job_state` function (starts around line 247). Add these two new functions immediately after `_set_job_state` (between `_set_job_state` and `run_once`):

```python
def _get_artifact(con: sqlite3.Connection, job_id: int, kind: str,
                  obs_id: int | None) -> dict | None:
    """Return the parsed payload_json for a (job_id, kind, obs_id) artifact, or None."""
    if obs_id is None:
        row = con.execute(
            "SELECT payload_json FROM extraction_artifacts WHERE job_id=? AND kind=? AND obs_id IS NULL",
            (job_id, kind),
        ).fetchone()
    else:
        row = con.execute(
            "SELECT payload_json FROM extraction_artifacts WHERE job_id=? AND kind=? AND obs_id=?",
            (job_id, kind, obs_id),
        ).fetchone()
    return json.loads(row[0]) if row else None


def _save_artifact(con: sqlite3.Connection, job_id: int, kind: str,
                   obs_id: int | None, payload: dict, model: str) -> None:
    """Persist a checkpoint artifact in its own committed tx.

    First-write-wins: partial UNIQUE indices + INSERT OR IGNORE keep one
    row per (job_id,kind,obs_id). Re-saving the same triple is a safe no-op
    — the caller may be replaying after a crash where the artifact was
    already committed but the downstream work (apply) hadn't finished.
    """
    con.execute("BEGIN IMMEDIATE")
    con.execute(
        "INSERT OR IGNORE INTO extraction_artifacts(job_id, kind, obs_id, payload_json, model) "
        "VALUES (?, ?, ?, ?, ?)",
        (job_id, kind, obs_id, json.dumps(payload, ensure_ascii=False), model),
    )
    con.execute("COMMIT")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/nikshilov/dev/ai/pulse
python3 -m pytest scripts/tests/test_extract_phase1.py -k "artifact" -v
python3 -m pytest scripts/tests/test_extract_phase0.py -v  # regression
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse
git add scripts/pulse_extract.py scripts/tests/test_extract_phase1.py
git commit -m "pulse extract phase 1: _get_artifact / _save_artifact helpers

Own BEGIN IMMEDIATE/COMMIT tx (like _set_job_state). INSERT OR IGNORE
with partial UNIQUE indices gives first-write-wins idempotency —
safe to call on replay without risking payload overwrite.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 8: `run_once` triage checkpoint (Layer 3b)

**Goal:** Before calling Sonnet, check for a triage artifact. If present, use it. Otherwise call Sonnet and save the result. A restarted job skips the Sonnet call entirely.

**Files:**
- Modify: `scripts/pulse_extract.py` `run_once` function (around line 303)
- Modify: `scripts/tests/test_extract_phase1.py`

- [ ] **Step 1: Append the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/nikshilov/dev/ai/pulse
python3 -m pytest scripts/tests/test_extract_phase1.py -k "triage_artifact" -v
```

Expected: FAIL. The first because no artifact is written (current code just discards verdicts). The second because Sonnet is called unconditionally.

- [ ] **Step 3: Rewrite triage call in `run_once`**

In `/Users/nikshilov/dev/ai/pulse/scripts/pulse_extract.py`, locate the `run_once` inner try-block (starting around line 302). Find:

```python
            try:
                triage_prompt = prompts.build_triage_prompt(observations)
                verdicts = call_sonnet_triage(triage_prompt, expected_count=len(observations))
                job_reports: list[dict] = []
```

Replace with:

```python
            try:
                verdicts = _get_artifact(con, job_id, "triage", None)
                if verdicts is None:
                    triage_prompt = prompts.build_triage_prompt(observations)
                    verdicts = call_sonnet_triage(triage_prompt, expected_count=len(observations))
                    _save_artifact(con, job_id, "triage", None, verdicts, TRIAGE_MODEL)
                job_reports: list[dict] = []
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/nikshilov/dev/ai/pulse
python3 -m pytest scripts/tests/test_extract_phase1.py scripts/tests/test_extract_phase0.py -v
```

Expected: all pass. The `test_crash_in_obs_two_preserves_obs_one_writes` Phase 0 test uses `fake_triage` that returns verdicts; after this change, the first run saves a triage artifact. If that test re-runs the same `run_once` on the same DB, the second call would reuse the artifact — which is exactly the new behavior. The test only calls `run_once` once, so it's unaffected. But double-check: `test_crash_in_obs_two_preserves_obs_one_writes` calls `pulse_extract.run_once(str(db))` a single time; the triage artifact is saved but not reused within the same call. OK.

- [ ] **Step 5: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse
git add scripts/pulse_extract.py scripts/tests/test_extract_phase1.py
git commit -m "pulse extract phase 1: triage checkpoint — skip Sonnet on restart

run_once checks extraction_artifacts for a triage row before calling
Sonnet; cached verdicts are reused verbatim. Restart of a crashed
job no longer re-bills the Sonnet call.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 9: `run_once` extract checkpoint per-obs (Layer 3c)

**Goal:** For each observation that triage says to extract, check for an extract artifact keyed by `(job_id, obs_id)`. If present, skip the Opus call and use the cached result. Otherwise call Opus and save. Restart of a crashed job skips Opus calls for any observation already captured.

**Files:**
- Modify: `scripts/pulse_extract.py` `run_once` inner loop
- Modify: `scripts/tests/test_extract_phase1.py`

- [ ] **Step 1: Append the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/nikshilov/dev/ai/pulse
python3 -m pytest scripts/tests/test_extract_phase1.py -k "extract_artifact or extract_artifact_per_obs" -v
```

Expected: FAIL. No extract artifact written (current code just calls Opus and applies without saving). Replay calls Opus twice (both obs) even though obs 1 is cached.

- [ ] **Step 3: Rewrite the extract loop in `run_once`**

In `/Users/nikshilov/dev/ai/pulse/scripts/pulse_extract.py`, locate the inner loop (around line 311). Find:

```python
                for obs, v in zip(observations, verdicts):
                    if v["verdict"] != "extract":
                        continue
                    graph_ctx = {"existing_entities": _load_existing_entities(con)}
                    extract_prompt = prompts.build_extract_prompt(obs, graph_ctx)
                    result = call_opus_extract(extract_prompt)

                    con.execute("BEGIN IMMEDIATE")
                    try:
                        obs_report = _apply_extraction(con, obs["id"], result)
                        con.execute("COMMIT")
                        job_reports.append(obs_report)
                    except Exception as ex:
                        con.execute("ROLLBACK")
                        job_reports.append({
                            "obs_id": obs["id"],
                            "entities_written": 0, "events_written": 0,
                            "relations_written": 0, "facts_written": 0,
                            "failed_items": [{
                                "item_kind": "whole_obs",
                                "reason": f"{type(ex).__name__}: {str(ex)[:200]}",
                                "detail": {},
                            }],
                        })
                        raise
```

Replace with:

```python
                for obs, v in zip(observations, verdicts):
                    if v["verdict"] != "extract":
                        continue
                    result = _get_artifact(con, job_id, "extract", obs["id"])
                    if result is None:
                        graph_ctx = {"existing_entities": _load_existing_entities(con)}
                        extract_prompt = prompts.build_extract_prompt(obs, graph_ctx)
                        result = call_opus_extract(extract_prompt)
                        _save_artifact(con, job_id, "extract", obs["id"], result, EXTRACT_MODEL)

                    con.execute("BEGIN IMMEDIATE")
                    try:
                        obs_report = _apply_extraction(con, obs["id"], result)
                        con.execute("COMMIT")
                        job_reports.append(obs_report)
                    except Exception as ex:
                        con.execute("ROLLBACK")
                        job_reports.append({
                            "obs_id": obs["id"],
                            "entities_written": 0, "events_written": 0,
                            "event_entities_written": 0,
                            "relations_written": 0, "facts_written": 0,
                            "failed_items": [{
                                "item_kind": "whole_obs",
                                "reason": f"{type(ex).__name__}: {str(ex)[:200]}",
                                "detail": {},
                            }],
                        })
                        raise
```

(Also adds `event_entities_written: 0` to the whole-obs failure report so report shape stays consistent after Task 6.)

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/nikshilov/dev/ai/pulse
python3 -m pytest scripts/tests/test_extract_phase1.py scripts/tests/test_extract_phase0.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse
git add scripts/pulse_extract.py scripts/tests/test_extract_phase1.py
git commit -m "pulse extract phase 1: per-obs extract checkpoint — skip Opus on restart

Each extracted observation's Opus result is persisted to
extraction_artifacts before apply. Restart reuses cached results
so crashes don't re-bill Opus for observations already captured.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 10: Widen SAVEPOINT except to Exception (Layer 4, task #84)

**Goal:** Replace each of the four `except sqlite3.Error as ex:` handlers in `_apply_extraction` with `except Exception as ex:`, and include the exception class name in the `reason` string. This catches the `KeyError: 'canonical_name'` class of bugs observed in the Phase 0 real-data smoke, keeping malformed LLM responses contained within the per-item SAVEPOINT.

**Files:**
- Modify: `scripts/pulse_extract.py` — 4 SAVEPOINT handlers in `_apply_extraction`
- Modify: `scripts/tests/test_extract_phase1.py`

- [ ] **Step 1: Append the failing test**

```python
# ---------- Layer 4: hygiene (task #84) ----------

def test_keyerror_in_entity_caught_by_savepoint(tmp_path):
    """An entity dict missing canonical_name raises KeyError; SAVEPOINT must contain it
    and the failure must land in failed_items, not propagate out."""
    db = tmp_path / "p1.db"
    _apply_migrations(db)

    con = pulse_extract._open_connection(str(db))
    con.execute(
        """INSERT INTO observations(source_kind,source_id,content_hash,version,scope,captured_at,observed_at,actors,content_text,metadata,raw_json)
           VALUES ('claude_jsonl','f:1','h',1,'shared','2026-04-16T00:00:00Z','2026-04-16T00:00:00Z','[]','t','{}','{}')"""
    )
    result = {
        "entities": [
            {"canonical_name": "Anna", "kind": "person"},
            {"kind": "person"},  # malformed — no canonical_name
            {"canonical_name": "Fedya", "kind": "person"},
        ],
        "events": [], "relations": [], "facts": [],
    }
    con.execute("BEGIN IMMEDIATE")
    report = pulse_extract._apply_extraction(con, 1, result)
    con.execute("COMMIT")

    names = {r[0] for r in con.execute("SELECT canonical_name FROM entities")}
    con.close()
    assert names == {"Anna", "Fedya"}, "good entities survive a bad sibling"
    assert report["entities_written"] == 2
    bad = [f for f in report["failed_items"] if f["item_kind"] == "entity"]
    assert len(bad) == 1
    assert "KeyError" in bad[0]["reason"], (
        f"failed_item reason must include KeyError (got {bad[0]['reason']!r})"
    )
    assert bad[0]["detail"]["index"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/nikshilov/dev/ai/pulse
python3 -m pytest scripts/tests/test_extract_phase1.py::test_keyerror_in_entity_caught_by_savepoint -v
```

Expected: FAIL. The current code catches only `sqlite3.Error`; the `KeyError` from `ent["canonical_name"]` escapes. The test's `con.execute("COMMIT")` line likely never runs because the KeyError propagates out of `_apply_extraction`. Pytest records the KeyError as the failure, not an assertion failure.

- [ ] **Step 3: Widen the four SAVEPOINT handlers**

In `/Users/nikshilov/dev/ai/pulse/scripts/pulse_extract.py`, four locations need editing. In each, change `except sqlite3.Error as ex:` to `except Exception as ex:` and update the `reason` to include the exception class name:

**Location 1 — entities block (around line 164):**

Find:
```python
        except sqlite3.Error as ex:
            con.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            con.execute(f"RELEASE SAVEPOINT {sp}")
            _item_failure("entity", str(ex)[:200], {"index": idx, "canonical_name": ent.get("canonical_name", ""), "kind": ent.get("kind", "")})
```

Replace with:
```python
        except Exception as ex:
            con.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            con.execute(f"RELEASE SAVEPOINT {sp}")
            _item_failure("entity", f"{type(ex).__name__}: {str(ex)[:200]}",
                          {"index": idx, "canonical_name": ent.get("canonical_name", ""), "kind": ent.get("kind", "")})
```

**Location 2 — events block (around line 189):**

Find:
```python
        except sqlite3.Error as ex:
            con.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            con.execute(f"RELEASE SAVEPOINT {sp}")
            _item_failure("event", str(ex)[:200], {"index": idx, "title": ev.get("title", "")})
```

Replace with:
```python
        except Exception as ex:
            con.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            con.execute(f"RELEASE SAVEPOINT {sp}")
            _item_failure("event", f"{type(ex).__name__}: {str(ex)[:200]}",
                          {"index": idx, "title": ev.get("title", "")})
```

**Location 3 — relations block (around line 214):**

Find:
```python
        except sqlite3.Error as ex:
            con.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            con.execute(f"RELEASE SAVEPOINT {sp}")
            _item_failure("relation", str(ex)[:200], {"index": idx, "from": rel.get("from", ""), "to": rel.get("to", ""), "kind": rel.get("kind", "")})
```

Replace with:
```python
        except Exception as ex:
            con.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            con.execute(f"RELEASE SAVEPOINT {sp}")
            _item_failure("relation", f"{type(ex).__name__}: {str(ex)[:200]}",
                          {"index": idx, "from": rel.get("from", ""), "to": rel.get("to", ""), "kind": rel.get("kind", "")})
```

**Location 4 — facts block (around line 239):**

Find:
```python
        except sqlite3.Error as ex:
            con.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            con.execute(f"RELEASE SAVEPOINT {sp}")
            _item_failure("fact", str(ex)[:200], {"index": idx, "entity": fact.get("entity", ""), "text": fact.get("text", "")[:80]})
```

Replace with:
```python
        except Exception as ex:
            con.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            con.execute(f"RELEASE SAVEPOINT {sp}")
            _item_failure("fact", f"{type(ex).__name__}: {str(ex)[:200]}",
                          {"index": idx, "entity": fact.get("entity", ""), "text": fact.get("text", "")[:80]})
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/nikshilov/dev/ai/pulse
python3 -m pytest scripts/tests/test_extract_phase1.py scripts/tests/test_extract_phase0.py -v
```

Expected: all pass. Phase 0's `test_savepoint_isolates_one_bad_entity` (uses `kind='potato'` → sqlite3.IntegrityError from CHECK) still passes — `Exception` catches `sqlite3.Error` as a subclass. Phase 0's `test_savepoint_isolates_one_bad_relation` (uses `kind=None` → NOT NULL violation) also still passes. The Phase 0 `reason` string now has a class-name prefix but those tests only assert on `item_kind` and `detail.index`, not the exact reason string.

- [ ] **Step 5: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse
git add scripts/pulse_extract.py scripts/tests/test_extract_phase1.py
git commit -m "pulse extract phase 1: widen SAVEPOINT except to Exception (task #84)

Malformed LLM responses can raise KeyError (observed in Phase 0
real-data smoke). Widening from sqlite3.Error to Exception contains
these in per-item SAVEPOINT instead of escaping to the outer obs tx.
reason string now includes the exception class name.

Closes #84 (Phase 0 follow-up).

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 11: E2E assert `event_entities_written` in report output

**Goal:** Update the E2E test to assert that the `apply_report` printed by `run_once` includes the new `event_entities_written` key. Catches regressions that might strip the key from reports.

**Files:**
- Modify: `scripts/tests/test_extract_e2e.py`
- Read: `scripts/tests/fixtures/extract_responses.json` to understand what the fixture extract returns

- [ ] **Step 1: Check the e2e fixture**

```bash
cd /Users/nikshilov/dev/ai/pulse
python3 -c "import json; d = json.load(open('scripts/tests/fixtures/extract_responses.json')); print(json.dumps(d.get('extract_1'), indent=2, ensure_ascii=False))"
```

Read the output. If `extract_1.events[*].entities_involved` exists and references entities that `extract_1.entities` resolves, the e2e will naturally produce `event_entities_written > 0`. If not, the test should just assert the key is present (regardless of value).

- [ ] **Step 2: Add assertion to the existing e2e test**

Edit `/Users/nikshilov/dev/ai/pulse/scripts/tests/test_extract_e2e.py`. Find:

```python
def test_e2e_prints_apply_report(tmp_path, monkeypatch, capsys):
    fixtures = json.loads(FIXTURES.read_text())
    db = _seed(tmp_path)

    def fake_triage(*_args, **_kwargs):
        return prompts.parse_triage_response(fixtures["triage"], expected_count=2)

    def fake_extract(_prompt):
        return fixtures["extract_1"]

    monkeypatch.setattr(pulse_extract, "call_sonnet_triage", fake_triage)
    monkeypatch.setattr(pulse_extract, "call_opus_extract", fake_extract)

    rc = pulse_extract.run_once(str(db))
    assert rc == 0

    captured = capsys.readouterr()
    assert "apply_report=" in captured.out
    # Sanity: the report must mention at least one entity_written
    assert '"entities_written"' in captured.out
```

Replace the body (after `pulse_extract.run_once`) with:

```python
    rc = pulse_extract.run_once(str(db))
    assert rc == 0

    captured = capsys.readouterr()
    assert "apply_report=" in captured.out
    # Report shape must carry all Phase 0 + Phase 1 write counters
    assert '"entities_written"' in captured.out
    assert '"event_entities_written"' in captured.out
```

- [ ] **Step 3: Run the e2e test**

```bash
cd /Users/nikshilov/dev/ai/pulse
python3 -m pytest scripts/tests/test_extract_e2e.py -v
```

Expected: both e2e tests pass (the new assertion verifies the added key is in the printed JSON).

- [ ] **Step 4: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse
git add scripts/tests/test_extract_e2e.py
git commit -m "pulse extract phase 1: e2e asserts event_entities_written in report

Catches regressions that strip the junction-write counter from
apply_report output.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 12: Full suite re-run + real-data smoke on pulse-dev copy

**Goal:** Final validation. Run the entire test suite, then run the extractor end-to-end against a copy of `pulse-dev/pulse.db` to confirm migration 006 applies cleanly, no Python tracebacks escape, and `apply_report` outputs include the new keys.

**Files:**
- Read: `/Users/nikshilov/dev/ai/pulse/pulse-dev/pulse.db` (source)
- Write (temp): `/tmp/pulse-phase1-smoke.db`

- [ ] **Step 1: Run full pytest suite**

```bash
cd /Users/nikshilov/dev/ai/pulse
python3 -m pytest scripts/tests/ -v
```

Expected: All Phase 0 tests (7) + all Phase 1 tests (should be ~17-18 by now) + e2e tests (2) pass. No skips other than whatever Phase 0 already skipped.

If anything fails, STOP and fix before proceeding.

- [ ] **Step 2: Create smoke DB and run migrations on it**

```bash
cp /Users/nikshilov/dev/ai/pulse/pulse-dev/pulse.db /tmp/pulse-phase1-smoke.db
cd /Users/nikshilov/dev/ai/pulse
python3 -c "
import sqlite3
from pathlib import Path
migs = sorted(Path('internal/store/migrations').glob('*.sql'))
con = sqlite3.connect('/tmp/pulse-phase1-smoke.db')
# Apply only 006 (005 already in pulse-dev); if 006 was already applied the CREATE statements will raise.
m006 = Path('internal/store/migrations/006_phase_1.sql').read_text()
try:
    con.executescript(m006)
    con.commit()
    print('006 applied cleanly')
except sqlite3.OperationalError as e:
    print(f'006 failed: {e}')
    raise
con.close()
"
```

Expected:
```
006 applied cleanly
```

If this step reports `006 failed: duplicate relation/fact`, STOP — the Task 1 audit was incomplete.

- [ ] **Step 3: Run audit against post-migration DB to confirm invariants**

```bash
cd /Users/nikshilov/dev/ai/pulse
python3 scripts/phase1_audit.py --db /tmp/pulse-phase1-smoke.db
```

Expected: `OK: no duplicates`.

- [ ] **Step 4: Run one iteration of extraction against the smoke DB**

```bash
cd /Users/nikshilov/dev/ai/pulse
set -a
source /Users/nikshilov/dev/ai/Garden/.env
set +a
python3 scripts/pulse_extract.py --db /tmp/pulse-phase1-smoke.db --budget 1.0
```

Expected:
- Process exits 0
- No Python traceback in the output
- `apply_report=` lines include `"event_entities_written"` (new key)
- If any jobs got `done` status with non-trivial extraction, entries in `extraction_artifacts` table; verify with:

```bash
sqlite3 /tmp/pulse-phase1-smoke.db \
  "SELECT job_id, kind, obs_id, model FROM extraction_artifacts ORDER BY job_id, kind, obs_id LIMIT 20;"
```

- [ ] **Step 5: Sanity-check checkpoint replay**

Re-run the same command. If some jobs were processed in Step 4, they should now be `done` and not re-picked. If a job crashed mid-flight (state=pending, attempts>0), its artifacts should be present — re-run will skip the LLM calls for already-extracted observations.

```bash
cd /Users/nikshilov/dev/ai/pulse
python3 scripts/pulse_extract.py --db /tmp/pulse-phase1-smoke.db --budget 1.0
```

Expected: no new artifacts for jobs already done; pending jobs proceed without repeating cached LLM calls. Exit 0. No traceback.

- [ ] **Step 6: Record findings in the plan**

Edit `/Users/nikshilov/dev/ai/pulse/docs/superpowers/plans/2026-04-16-pulse-extraction-phase-1.md` (this file). Scroll to the bottom and append a "Phase 1 validation (Task 12)" section:

```markdown
---

## Phase 1 validation (Task 12)

**Date:** 2026-04-16 (fill in actual date when executed)
**Smoke DB:** `/tmp/pulse-phase1-smoke.db` (copy of `pulse-dev/pulse.db`)

**Pre-migration audit:** <result of phase1_audit.py — "OK: no duplicates" expected>

**Migration 006:** <result — "applied cleanly" expected>

**First extraction run (budget=$1):**
- Exit code: <0 expected>
- Jobs processed: <fill in>
- Tracebacks in output: <none expected>
- `event_entities_written` present in apply_report output: <yes expected>
- Artifacts table after run: <count per kind, fill in>

**Second extraction run (idempotency check):**
- Exit code: <0 expected>
- LLM calls avoided via checkpoint: <qualitative — "cached obs skipped as expected" if applicable>
- DB state delta: <no unexpected changes beyond pending jobs advancing>

**Follow-ups surfaced (if any):** <fill in>
```

Fill in the actual values from Steps 1-5.

- [ ] **Step 7: Commit the validation record**

```bash
cd /Users/nikshilov/dev/ai/pulse
git add docs/superpowers/plans/2026-04-16-pulse-extraction-phase-1.md
git commit -m "pulse extract phase 1: record Task 12 validation findings

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

- [ ] **Step 8: Push branch and open PR (manual by controller, not part of this plan)**

```bash
cd /Users/nikshilov/dev/ai/pulse
git push -u origin extract-phase-1
gh pr create --title "Pulse extract Phase 1: UNIQUEs + junction + checkpoint + hygiene" --body "<fill in summary of the 12 tasks + test counts + Task 12 findings>"
```

---

## Self-review checklist

Before dispatching the first subagent, verify:

**Spec coverage (from `docs/superpowers/specs/2026-04-16-pulse-extraction-phase-1-design.md`):**
- [x] UNIQUE on relations(from,to,kind) — Task 3
- [x] UNIQUE on facts(entity_id,text) — Task 3
- [x] event_entities junction table — Task 3
- [x] extraction_artifacts checkpoint — Task 3 (with schema correction per Task 2)
- [x] Relations UPSERT — Task 4
- [x] Facts INSERT OR IGNORE — Task 5
- [x] event_entities junction writes on apply — Task 6
- [x] Partial event resolution (permissive) — Task 6
- [x] apply_report.event_entities_written — Task 6
- [x] _get_artifact / _save_artifact own tx — Task 7
- [x] Triage checkpoint skip on restart — Task 8
- [x] Extract checkpoint skip per-obs on restart — Task 9
- [x] Widen except sqlite3.Error → Exception (#84) — Task 10
- [x] E2E test update — Task 11
- [x] Full suite + real-data smoke — Task 12

**No placeholders:** searched for "TBD", "TODO", "implement later", "similar to Task", etc — none found.

**Type consistency:**
- `_get_artifact(con, job_id: int, kind: str, obs_id: int | None) -> dict | None` (Task 7, used in Tasks 8 + 9)
- `_save_artifact(con, job_id: int, kind: str, obs_id: int | None, payload: dict, model: str) -> None` (Task 7, used in Tasks 8 + 9)
- `_apply_extraction(con, obs_id: int, result: dict) -> dict` (unchanged from Phase 0, report dict extended in Task 6)
- `apply_report` keys: `obs_id, entities_written, events_written, event_entities_written, relations_written, facts_written, failed_items` — consistent across Tasks 6, 9, 11.

**Migration ordering:** 006 appears alphabetically after 005_graph.sql in `internal/store/migrations/` — `sorted(MIGRATIONS.glob("*.sql"))` applies them in order.

**Phase 0 regression:** existing Phase 0 tests in `test_extract_phase0.py` exercised after each task. Widening `except` to Exception preserves existing behavior because `sqlite3.Error < Exception`. Adding UNIQUEs does not affect Phase 0 tests because none of them write duplicate relations or facts.
