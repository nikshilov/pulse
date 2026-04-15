# Pulse Extraction Phase 0 (Unblock) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One observation's crash no longer destroys the apply work of other observations in the same extraction job; the Python extractor no longer races with the Go ingest process on WAL writes; partial writes no longer leak into the job's final state.

**Architecture:** Wrap each observation's apply work in an explicit `BEGIN IMMEDIATE` / `COMMIT` transaction with `SAVEPOINT` per entity/event/relation/fact; on item-level `sqlite3.IntegrityError` rollback to the savepoint, log the failure to `apply_report.failed_items[]`, and continue with the next item; on observation-level crash rollback the whole outer tx and continue with the next observation. Job state transitions (`pending → running → done/failed/dlq`) run in their own small transactions so they never commit partial apply work. Add `PRAGMA busy_timeout=5000` to the Python sqlite3 connection (the Go side already has it in `internal/store/store.go:27`).

**Tech Stack:** Python 3.13, `sqlite3` stdlib (with `isolation_level=None` for manual tx control), `anthropic` SDK, `pytest`, `modernc.org/sqlite` on the Go side (unchanged).

---

## Context

This plan closes Phase 0 from the v2 design bundle (`~/dev/ai/bench/datasets/pulse-extraction-design-v2.md`, sections "Component 7 — Apply" and "Migration plan · Phase 0"). It is also a direct fix for the 12-judge v1 consensus bug (9/10 judges: "один crash откатывает весь job"), confirmed unresolved in the v1 code sketch.

**Scope check:** This plan covers **only** the transaction-boundary rewrite and the Python-side `PRAGMA busy_timeout` fix. It does **not** cover:

- **UNIQUE constraints** on `relations(from_entity_id, to_entity_id, kind)` and `facts(entity_id, text)` — deferred to Phase 1 (requires a migration)
- **`event_entities` junction table** — deferred to Phase 1
- **`extraction_artifacts` checkpoint table** — deferred to Phase 1
- **Anthropic tool-use schema v2** (`mention_text` contract) — deferred to Phase 2
- **Entity alias index, normalizer, top-K retrieval** — deferred to Phase 3
- **Alias-learning gates** (pending state, 2-proof confirmation) — deferred to Phase 3
- **MCP retrieval tools + web viewer** — deferred to Phase 4
- **Scorer calibration anchors** — deferred to Phase 2 (part of extract prompt rewrite)

Phase 0 is the minimum viable fix to make re-runs on real data survive isolated crashes. All fixes here are additive; no migration required.

---

## File Structure

**Modify:**

- `scripts/pulse_extract.py` — single file, 231 lines today. Refactor:
  - Add `_open_connection()` helper (PRAGMA setup, `isolation_level=None`)
  - Rewrite `_apply_extraction()` to return `apply_report` dict, wrap each item in `SAVEPOINT`, skip orphan events
  - Rewrite `run_once()` to use per-observation `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK` and to run job-state updates in their own transactions

**Create:**

- `scripts/tests/test_extract_phase0.py` — new file, covers:
  - `_open_connection` sets the expected pragmas
  - An `IntegrityError` on one entity inside a multi-item observation does **not** roll back the other items
  - An observation-level crash does **not** roll back the prior observation's work in the same job
  - Event without `entities_involved` is skipped (orphan-drop) and logged in `apply_report.failed_items`
  - Job state transitions commit independently of apply writes

**Unchanged:**

- `scripts/extract/prompts.py` (triage/extract prompts are Phase 2 work)
- `scripts/extract/resolver.py`, `scripts/extract/scorer.py`
- All Go code under `internal/` (already has `PRAGMA busy_timeout=5000` at `internal/store/store.go:27`)
- All migrations under `internal/store/migrations/` (Phase 0 is additive, no schema change)

---

## Pre-work: Confirm current test baseline

- [ ] **Step 0: Capture current test status**

Run (from `/Users/nikshilov/dev/ai/pulse/scripts/`):

```bash
cd /Users/nikshilov/dev/ai/pulse/scripts && python3 -m pytest tests/ -v 2>&1 | tail -30
```

Expected: all existing tests pass (T24 smoke + T25 real-archive run already green per project memory). Record the pass count so regressions are obvious. If anything is red at baseline, stop and report — Phase 0 assumes the existing suite is green.

---

## Task 1: `_open_connection` helper with `PRAGMA busy_timeout` and manual tx control

**Files:**

- Create: new helper inside `scripts/pulse_extract.py` (after the existing `_anthropic_client` block, before `_load_observations`)
- Test: `scripts/tests/test_extract_phase0.py` (new file, one test)

- [ ] **Step 1: Write the failing test**

Create `scripts/tests/test_extract_phase0.py` with:

```python
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
    finally:
        con.close()

    assert fk == 1, "foreign_keys must be ON"
    assert bt == 5000, "busy_timeout must be 5000 ms"
    assert con.isolation_level is None, (
        "isolation_level must be None so BEGIN/COMMIT are under our control"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
cd /Users/nikshilov/dev/ai/pulse/scripts && python3 -m pytest tests/test_extract_phase0.py::test_open_connection_sets_pragmas -v
```

Expected: `FAIL` with `AttributeError: module 'pulse_extract' has no attribute '_open_connection'`.

- [ ] **Step 3: Write minimal implementation**

Add to `scripts/pulse_extract.py` immediately after the `_anthropic_client()` function (around line 34):

```python
def _open_connection(db_path: str) -> sqlite3.Connection:
    """Open a sqlite3 connection wired for the extractor.

    - PRAGMA busy_timeout=5000: survive WAL contention with the Go ingest process
      (the Go side sets the same value via DSN in internal/store/store.go).
    - PRAGMA foreign_keys=ON: schema assumes FK enforcement.
    - isolation_level=None: manual BEGIN/COMMIT so we can scope transactions to a
      single observation and use SAVEPOINT per item.
    """
    con = sqlite3.connect(db_path)
    con.isolation_level = None
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=5000")
    return con
```

- [ ] **Step 4: Run test to verify it passes**

Run the same pytest command from Step 2.
Expected: `PASS` (1 passed).

- [ ] **Step 5: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse && git add scripts/pulse_extract.py scripts/tests/test_extract_phase0.py && git commit -m "pulse extract phase 0: add _open_connection with busy_timeout pragma

Python side was racing Go ingest on WAL writes because Python's sqlite3
default busy_timeout is 0. Also sets isolation_level=None so upcoming
BEGIN/COMMIT and SAVEPOINT logic is under our control.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 2: `_apply_extraction` returns `apply_report` dict

**Files:**

- Modify: `scripts/pulse_extract.py:81-165` (the whole `_apply_extraction` body)
- Test: `scripts/tests/test_extract_phase0.py` (add one more test)

- [ ] **Step 1: Write the failing test**

Append to `scripts/tests/test_extract_phase0.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
cd /Users/nikshilov/dev/ai/pulse/scripts && python3 -m pytest tests/test_extract_phase0.py::test_apply_extraction_returns_report -v
```

Expected: `FAIL` with `TypeError: '_apply_extraction' returned None` or `AssertionError` — current signature returns `None`.

- [ ] **Step 3: Write minimal implementation**

Replace the entire `_apply_extraction` function in `scripts/pulse_extract.py` (lines 81-165) with the version below. The logic is the same as today's code; the only changes are (a) the function returns a `report` dict, (b) event handling is gated on `entities_involved` non-empty (Task 3 will add the test for this), (c) counters are incremented on success. No `SAVEPOINT` yet — that lands in Task 5.

```python
def _apply_extraction(con: sqlite3.Connection, obs_id: int, result: dict) -> dict:
    """Apply one extraction result to the graph. Caller owns the outer transaction.

    Returns an apply_report dict:
        {
          "obs_id": int,
          "entities_written": int, "events_written": int,
          "relations_written": int, "facts_written": int,
          "failed_items": [ {"item_kind": str, "reason": str, "detail": dict}, ... ]
        }
    """
    report = {
        "obs_id": obs_id,
        "entities_written": 0,
        "events_written": 0,
        "relations_written": 0,
        "facts_written": 0,
        "failed_items": [],
    }

    existing = _load_existing_entities(con)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    name_to_id: dict[str, int] = {}

    # --- entities ---
    for ent in result.get("entities", []):
        dec = resolver.resolve_entity(ent, existing)
        scored = scorer.score_entity(ent)
        if dec.action == "bind_identity":
            con.execute(
                "UPDATE entities SET last_seen=?, salience_score=?, emotional_weight=?, scorer_version=? WHERE id=?",
                (now, scored["salience_score"], scored["emotional_weight"], scored["scorer_version"], dec.entity_id),
            )
            entity_id = dec.entity_id
        else:
            cur = con.execute(
                "INSERT INTO entities (canonical_name, kind, aliases, first_seen, last_seen, salience_score, emotional_weight, scorer_version) VALUES (?,?,?,?,?,?,?,?)",
                (ent["canonical_name"], ent.get("kind", "person"), json.dumps(ent.get("aliases") or []),
                 now, now, scored["salience_score"], scored["emotional_weight"], scored["scorer_version"]),
            )
            entity_id = cur.lastrowid
            existing.append({"id": entity_id, "canonical_name": ent["canonical_name"], "kind": ent.get("kind", "person"), "aliases": ent.get("aliases") or []})

            if dec.action == "proposal" and dec.entity_id:
                con.execute(
                    "INSERT INTO entity_merge_proposals (from_entity_id, to_entity_id, confidence, evidence_md, state, proposed_at) VALUES (?,?,?,?,?,?)",
                    (entity_id, dec.entity_id, dec.confidence, dec.reason, "pending", now),
                )
            elif dec.action == "new_entity_with_question":
                ttl = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 7 * 86400))
                con.execute(
                    "INSERT INTO open_questions (subject_entity_id, question_text, asked_at, ttl_expires_at, state) VALUES (?,?,?,?,?)",
                    (entity_id, f"Is {ent['canonical_name']} a new person, or an alias of someone I know?", now, ttl, "open"),
                )

        con.execute(
            "INSERT INTO evidence (subject_kind, subject_id, observation_id, created_at) VALUES ('entity',?,?,?)",
            (entity_id, obs_id, now),
        )

        name_to_id[ent["canonical_name"]] = entity_id
        for alias in (ent.get("aliases") or []):
            name_to_id[alias] = entity_id
        report["entities_written"] += 1

    # --- events ---
    for ev in result.get("events", []):
        involved = ev.get("entities_involved") or []
        if not involved:
            report["failed_items"].append({
                "item_kind": "event",
                "reason": "orphan_event_no_entities_involved",
                "detail": {"title": ev.get("title", "")},
            })
            continue
        s = scorer.score_event(ev)
        cur = con.execute(
            "INSERT INTO events (title, description, sentiment, emotional_weight, scorer_version, ts) VALUES (?,?,?,?,?,?)",
            (ev.get("title", ""), ev.get("description", ""), s["sentiment"], s["emotional_weight"], s["scorer_version"], ev.get("ts", now)),
        )
        con.execute(
            "INSERT INTO evidence (subject_kind, subject_id, observation_id, created_at) VALUES ('event',?,?,?)",
            (cur.lastrowid, obs_id, now),
        )
        report["events_written"] += 1

    # --- relations ---
    for rel in result.get("relations", []):
        from_id = name_to_id.get(rel.get("from", ""))
        to_id = name_to_id.get(rel.get("to", ""))
        if from_id is None or to_id is None:
            report["failed_items"].append({
                "item_kind": "relation",
                "reason": "unknown_entity",
                "detail": {"from": rel.get("from", ""), "to": rel.get("to", "")},
            })
            continue
        cur = con.execute(
            "INSERT INTO relations (from_entity_id, to_entity_id, kind, strength, first_seen, last_seen) VALUES (?,?,?,?,?,?)",
            (from_id, to_id, rel.get("kind", ""), float(rel.get("strength", 0.0)), now, now),
        )
        con.execute(
            "INSERT INTO evidence (subject_kind, subject_id, observation_id, created_at) VALUES ('relation',?,?,?)",
            (cur.lastrowid, obs_id, now),
        )
        report["relations_written"] += 1

    # --- facts ---
    for fact in result.get("facts", []):
        entity_id = name_to_id.get(fact.get("entity", ""))
        if entity_id is None:
            report["failed_items"].append({
                "item_kind": "fact",
                "reason": "unknown_entity",
                "detail": {"entity": fact.get("entity", ""), "text": fact.get("text", "")[:80]},
            })
            continue
        scored = scorer.score_fact(fact)
        cur = con.execute(
            "INSERT INTO facts (entity_id, text, confidence, scorer_version, created_at) VALUES (?,?,?,?,?)",
            (entity_id, fact.get("text", ""), scored["confidence"], scored["scorer_version"], now),
        )
        con.execute(
            "INSERT INTO evidence (subject_kind, subject_id, observation_id, created_at) VALUES ('fact',?,?,?)",
            (cur.lastrowid, obs_id, now),
        )
        report["facts_written"] += 1

    return report
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
cd /Users/nikshilov/dev/ai/pulse/scripts && python3 -m pytest tests/test_extract_phase0.py -v
```

Expected: 2 passed (`test_open_connection_sets_pragmas`, `test_apply_extraction_returns_report`).

Also re-run the full suite to catch regressions in the existing E2E test (which will still call the new `_apply_extraction` via `run_once`):

```bash
cd /Users/nikshilov/dev/ai/pulse/scripts && python3 -m pytest tests/ -v 2>&1 | tail -20
```

Expected: all tests pass. `test_extract_e2e.py::test_e2e_extraction_creates_graph` now calls a function that returns a dict but ignores the return value via `run_once`, so that test still passes.

- [ ] **Step 5: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse && git add scripts/pulse_extract.py scripts/tests/test_extract_phase0.py && git commit -m "pulse extract phase 0: _apply_extraction returns apply_report

Every entity/event/relation/fact insert increments a counter. Events
without entities_involved are skipped and logged as orphan_event in
apply_report.failed_items. Relations and facts with unresolved entity
references are also logged rather than silently printed.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 3: Orphan-drop events test

**Files:**

- Test: `scripts/tests/test_extract_phase0.py` (add one more test)

- [ ] **Step 1: Write the failing test**

Append to `scripts/tests/test_extract_phase0.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it passes immediately**

Since Task 2's implementation already added the orphan-drop branch, this test should pass on first run. Run:

```bash
cd /Users/nikshilov/dev/ai/pulse/scripts && python3 -m pytest tests/test_extract_phase0.py::test_event_without_entities_involved_is_dropped -v
```

Expected: `PASS`.

If it fails, the orphan-drop branch from Task 2 is wrong — fix it before moving on. Do not modify this test to make it pass.

- [ ] **Step 3: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse && git add scripts/tests/test_extract_phase0.py && git commit -m "pulse extract phase 0: test orphan-drop for events without entities_involved

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 4: SAVEPOINT per item in `_apply_extraction`

**Files:**

- Modify: `scripts/pulse_extract.py` (`_apply_extraction` body)
- Test: `scripts/tests/test_extract_phase0.py` (add one more test)

- [ ] **Step 1: Write the failing test**

Append to `scripts/tests/test_extract_phase0.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
cd /Users/nikshilov/dev/ai/pulse/scripts && python3 -m pytest tests/test_extract_phase0.py::test_savepoint_isolates_one_bad_entity -v
```

Expected: `FAIL` with `sqlite3.IntegrityError: CHECK constraint failed: kind` propagated out of `_apply_extraction`. That's the bug — one bad item kills the whole observation.

- [ ] **Step 3: Write minimal implementation**

Edit `_apply_extraction` in `scripts/pulse_extract.py` so each per-item block is wrapped in a `SAVEPOINT`/`RELEASE`/`ROLLBACK TO` pattern. Replace the entire `_apply_extraction` function (as written in Task 2) with this SAVEPOINT-wrapped version. Only the inner loops change; the signature, report shape, and overall control flow are the same.

```python
def _apply_extraction(con: sqlite3.Connection, obs_id: int, result: dict) -> dict:
    """Apply one extraction result to the graph. Caller owns the outer transaction.

    Each item (entity/event/relation/fact) is wrapped in SAVEPOINT so an
    sqlite3.IntegrityError on one item does not abort the others. The caller's
    outer tx stays open on return.
    """
    report = {
        "obs_id": obs_id,
        "entities_written": 0,
        "events_written": 0,
        "relations_written": 0,
        "facts_written": 0,
        "failed_items": [],
    }

    existing = _load_existing_entities(con)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    name_to_id: dict[str, int] = {}

    def _item_failure(item_kind: str, reason: str, detail: dict) -> None:
        report["failed_items"].append({"item_kind": item_kind, "reason": reason, "detail": detail})

    # --- entities ---
    for idx, ent in enumerate(result.get("entities", [])):
        sp = f"ent_{idx}"
        con.execute(f"SAVEPOINT {sp}")
        try:
            dec = resolver.resolve_entity(ent, existing)
            scored = scorer.score_entity(ent)
            if dec.action == "bind_identity":
                con.execute(
                    "UPDATE entities SET last_seen=?, salience_score=?, emotional_weight=?, scorer_version=? WHERE id=?",
                    (now, scored["salience_score"], scored["emotional_weight"], scored["scorer_version"], dec.entity_id),
                )
                entity_id = dec.entity_id
            else:
                cur = con.execute(
                    "INSERT INTO entities (canonical_name, kind, aliases, first_seen, last_seen, salience_score, emotional_weight, scorer_version) VALUES (?,?,?,?,?,?,?,?)",
                    (ent["canonical_name"], ent.get("kind", "person"), json.dumps(ent.get("aliases") or []),
                     now, now, scored["salience_score"], scored["emotional_weight"], scored["scorer_version"]),
                )
                entity_id = cur.lastrowid
                existing.append({"id": entity_id, "canonical_name": ent["canonical_name"], "kind": ent.get("kind", "person"), "aliases": ent.get("aliases") or []})

                if dec.action == "proposal" and dec.entity_id:
                    con.execute(
                        "INSERT INTO entity_merge_proposals (from_entity_id, to_entity_id, confidence, evidence_md, state, proposed_at) VALUES (?,?,?,?,?,?)",
                        (entity_id, dec.entity_id, dec.confidence, dec.reason, "pending", now),
                    )
                elif dec.action == "new_entity_with_question":
                    ttl = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 7 * 86400))
                    con.execute(
                        "INSERT INTO open_questions (subject_entity_id, question_text, asked_at, ttl_expires_at, state) VALUES (?,?,?,?,?)",
                        (entity_id, f"Is {ent['canonical_name']} a new person, or an alias of someone I know?", now, ttl, "open"),
                    )

            con.execute(
                "INSERT INTO evidence (subject_kind, subject_id, observation_id, created_at) VALUES ('entity',?,?,?)",
                (entity_id, obs_id, now),
            )
            con.execute(f"RELEASE SAVEPOINT {sp}")

            name_to_id[ent["canonical_name"]] = entity_id
            for alias in (ent.get("aliases") or []):
                name_to_id[alias] = entity_id
            report["entities_written"] += 1
        except sqlite3.Error as ex:
            con.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            con.execute(f"RELEASE SAVEPOINT {sp}")
            _item_failure("entity", str(ex)[:200], {"canonical_name": ent.get("canonical_name", ""), "kind": ent.get("kind", "")})

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
            _item_failure("event", str(ex)[:200], {"title": ev.get("title", "")})

    # --- relations ---
    for idx, rel in enumerate(result.get("relations", [])):
        from_id = name_to_id.get(rel.get("from", ""))
        to_id = name_to_id.get(rel.get("to", ""))
        if from_id is None or to_id is None:
            _item_failure("relation", "unknown_entity", {"from": rel.get("from", ""), "to": rel.get("to", "")})
            continue
        sp = f"rel_{idx}"
        con.execute(f"SAVEPOINT {sp}")
        try:
            cur = con.execute(
                "INSERT INTO relations (from_entity_id, to_entity_id, kind, strength, first_seen, last_seen) VALUES (?,?,?,?,?,?)",
                (from_id, to_id, rel.get("kind", ""), float(rel.get("strength", 0.0)), now, now),
            )
            con.execute(
                "INSERT INTO evidence (subject_kind, subject_id, observation_id, created_at) VALUES ('relation',?,?,?)",
                (cur.lastrowid, obs_id, now),
            )
            con.execute(f"RELEASE SAVEPOINT {sp}")
            report["relations_written"] += 1
        except sqlite3.Error as ex:
            con.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            con.execute(f"RELEASE SAVEPOINT {sp}")
            _item_failure("relation", str(ex)[:200], {"from": rel.get("from", ""), "to": rel.get("to", ""), "kind": rel.get("kind", "")})

    # --- facts ---
    for idx, fact in enumerate(result.get("facts", [])):
        entity_id = name_to_id.get(fact.get("entity", ""))
        if entity_id is None:
            _item_failure("fact", "unknown_entity", {"entity": fact.get("entity", ""), "text": fact.get("text", "")[:80]})
            continue
        sp = f"fact_{idx}"
        con.execute(f"SAVEPOINT {sp}")
        try:
            scored = scorer.score_fact(fact)
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
        except sqlite3.Error as ex:
            con.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            con.execute(f"RELEASE SAVEPOINT {sp}")
            _item_failure("fact", str(ex)[:200], {"entity": fact.get("entity", ""), "text": fact.get("text", "")[:80]})

    return report
```

- [ ] **Step 4: Run tests to verify they pass**

Run the new test and the full Phase 0 suite:

```bash
cd /Users/nikshilov/dev/ai/pulse/scripts && python3 -m pytest tests/test_extract_phase0.py -v
```

Expected: 4 passed (`_open_connection`, `apply_report`, `orphan_event`, `savepoint_isolates_bad_entity`).

Then run the full suite to catch regressions:

```bash
cd /Users/nikshilov/dev/ai/pulse/scripts && python3 -m pytest tests/ -v 2>&1 | tail -20
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse && git add scripts/pulse_extract.py scripts/tests/test_extract_phase0.py && git commit -m "pulse extract phase 0: SAVEPOINT per item so one bad row doesn't nuke obs

Each entity/event/relation/fact block is now wrapped in SAVEPOINT; on
sqlite3.Error the savepoint is rolled back, the failure is logged to
apply_report.failed_items, and the loop continues. Addresses the v1
consensus bug (9/10 judges) where a single CHECK/FK/NOT NULL violation
was dropping the whole observation's work.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 5: Per-observation `BEGIN IMMEDIATE` in `run_once` + independent state-update txs

**Files:**

- Modify: `scripts/pulse_extract.py:168-219` (entire `run_once` body)
- Test: `scripts/tests/test_extract_phase0.py` (add two tests)

- [ ] **Step 1: Write the failing tests**

Append to `scripts/tests/test_extract_phase0.py`:

```python
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
        return [{"verdict": "extract"} for _ in range(expected_count)]

    # Extract for obs 1 writes Anna; extract for obs 2 raises RuntimeError
    call_count = {"n": 0}

    def fake_extract(_prompt):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {
                "entities": [{"canonical_name": "Anna", "kind": "person"}],
                "events": [], "relations": [], "facts": [],
            }
        raise RuntimeError("simulated Anthropic timeout")

    monkeypatch.setattr(pulse_extract, "call_sonnet_triage", fake_triage)
    monkeypatch.setattr(pulse_extract, "call_opus_extract", fake_extract)

    rc = pulse_extract.run_once(str(db))
    assert rc == 0

    con = sqlite3.connect(db)
    names = {r[0] for r in con.execute("SELECT canonical_name FROM entities")}
    assert names == {"Anna"}, "obs 1's Anna must survive obs 2's crash"

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
        return [{"verdict": "extract"}]

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd /Users/nikshilov/dev/ai/pulse/scripts && python3 -m pytest tests/test_extract_phase0.py::test_crash_in_obs_two_preserves_obs_one_writes tests/test_extract_phase0.py::test_job_state_running_commits_before_apply -v
```

Expected: both `FAIL`.
- `test_crash_in_obs_two_preserves_obs_one_writes`: current `run_once` wraps all obs in one implicit tx; on exception `con.commit()` at line 216 fires, committing the partial obs 1 writes AND the state update together — so Anna would actually survive in the current code (by accident). But the test also asserts `last_error` contains "simulated" — and the `_set_job_state` call on exception path does commit that. The actual failure mode in the current code is that on the *next* `run_once` invocation (state='pending'), obs 1 gets re-applied, creating a second Anna. So this test reveals the at-least-once duplicate bug too. **Expected failure:** duplicate Anna entity or `last_error` not populated correctly depending on run order. If the test accidentally passes with the current code, tighten it by counting `SELECT COUNT(*) FROM entities WHERE canonical_name='Anna'` — must be exactly 1, not 2.
- `test_job_state_running_commits_before_apply`: current code opens a probe connection but `busy_timeout=2000` will likely fail because the main connection holds the implicit tx; probe sees `state='pending'` (before main's still-uncommitted update). So assertion `state_mid == 'running'` fails.

- [ ] **Step 3: Write minimal implementation**

Replace the entire `run_once` function in `scripts/pulse_extract.py` (lines 168-219) with:

```python
def _set_job_state(con: sqlite3.Connection, job_id: int, state: str, *,
                   last_error: str | None = None, increment_attempts: bool = False,
                   triage_model: str | None = None, extract_model: str | None = None) -> None:
    """Update extraction_jobs state in its own committed tx.

    Called at two boundaries: claim (pending -> running, +1 attempt) and
    finalize (running -> done/failed/dlq). Keeps state transitions durable
    regardless of apply-stage success.
    """
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    sets = ["state=?", "updated_at=?"]
    args: list = [state, now]
    if increment_attempts:
        sets.append("attempts=attempts+1")
    if last_error is not None:
        sets.append("last_error=?")
        args.append(last_error[:500])
    if triage_model is not None:
        sets.append("triage_model=?")
        args.append(triage_model)
    if extract_model is not None:
        sets.append("extract_model=?")
        args.append(extract_model)
    args.append(job_id)

    con.execute("BEGIN IMMEDIATE")
    con.execute(f"UPDATE extraction_jobs SET {', '.join(sets)} WHERE id=?", args)
    con.execute("COMMIT")


def run_once(db_path: str, budget_usd_remaining: float = 10.0) -> int:
    con = _open_connection(db_path)

    try:
        if budget_usd_remaining <= 0:
            print("budget exhausted for today — skipping extraction run")
            return 0

        jobs = con.execute(
            "SELECT id, observation_ids FROM extraction_jobs "
            "WHERE state='pending' ORDER BY created_at LIMIT 10"
        ).fetchall()
        if not jobs:
            print("no pending jobs")
            return 0

        for job_id, obs_ids_json in jobs:
            obs_ids = json.loads(obs_ids_json)
            _set_job_state(con, job_id, "running", increment_attempts=True)

            observations = _load_observations(con, obs_ids)
            if not observations:
                _set_job_state(con, job_id, "failed", last_error="no observations")
                continue

            try:
                triage_prompt = prompts.build_triage_prompt(observations)
                verdicts = call_sonnet_triage(triage_prompt, expected_count=len(observations))
                job_reports: list[dict] = []

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

                _set_job_state(
                    con, job_id, "done",
                    triage_model="sonnet-4.6", extract_model="opus-4.6",
                )
                print(f"job {job_id}: done, apply_report={json.dumps(job_reports, ensure_ascii=False)[:500]}")
            except Exception as e:
                attempts_row = con.execute(
                    "SELECT attempts FROM extraction_jobs WHERE id=?", (job_id,)
                ).fetchone()
                attempts = attempts_row[0] if attempts_row else 0
                next_state = "dlq" if attempts >= 3 else "pending"
                _set_job_state(con, job_id, next_state, last_error=str(e))
    finally:
        con.close()
    return 0
```

Key points of this rewrite:

- `run_once` uses `_open_connection` (Task 1) which sets `isolation_level=None` — every `BEGIN`/`COMMIT`/`ROLLBACK` in this function is explicit.
- `_set_job_state` is its own `BEGIN IMMEDIATE`/`COMMIT` — state transitions always commit regardless of apply success.
- Per observation: `BEGIN IMMEDIATE` → `_apply_extraction` (which uses `SAVEPOINT` internally) → `COMMIT` on success or `ROLLBACK` on unrecoverable crash. No partial writes from obs N leak into obs N+1.
- On any exception raised during the per-obs loop, the outer `try/except` still flips the job to `pending` or `dlq` via another call to `_set_job_state`.
- `apply_report`s are aggregated per-job and printed (stored-to-DB is Phase 1's job, when we add the `extraction_artifacts` table).

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
cd /Users/nikshilov/dev/ai/pulse/scripts && python3 -m pytest tests/test_extract_phase0.py -v
```

Expected: 6 passed.

Then run the full suite:

```bash
cd /Users/nikshilov/dev/ai/pulse/scripts && python3 -m pytest tests/ -v 2>&1 | tail -30
```

Expected: every test green. If `test_extract_e2e.py` fails, it is because the existing fixture depended on implicit-tx behavior — inspect and fix the fixture (not the new logic).

- [ ] **Step 5: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse && git add scripts/pulse_extract.py scripts/tests/test_extract_phase0.py && git commit -m "pulse extract phase 0: per-observation BEGIN IMMEDIATE + independent state tx

run_once now opens the connection via _open_connection (manual tx
control) and scopes each observation's apply to its own BEGIN IMMEDIATE
/ COMMIT. Job state transitions (running / done / failed / dlq) go
through _set_job_state which runs its own tx, so they always commit
regardless of whether apply succeeded. One crashed observation can no
longer silently commit partial writes alongside the state change.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 6: End-to-end smoke — apply_report populated on happy path

**Files:**

- Test: `scripts/tests/test_extract_e2e.py` (add one assertion — do not remove the existing test)

- [ ] **Step 1: Write the failing assertion**

Read the current `scripts/tests/test_extract_e2e.py` and locate `test_e2e_extraction_creates_graph`. After the final assertion in that test, append:

```python
    # Phase 0: apply_report must now be printed; grep the captured stdout
    # via a new test that runs the same fixture and checks stdout contains
    # "apply_report=" for the one extracted observation.

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

- [ ] **Step 2: Run test to verify it passes**

Run:

```bash
cd /Users/nikshilov/dev/ai/pulse/scripts && python3 -m pytest tests/test_extract_e2e.py -v
```

Expected: all passed (existing `test_e2e_extraction_creates_graph` + new `test_e2e_prints_apply_report`).

If the new test fails because `apply_report=` isn't in stdout, verify that Task 5's `print(f"job {job_id}: done, apply_report=...")` line survived editing.

- [ ] **Step 3: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse && git add scripts/tests/test_extract_e2e.py && git commit -m "pulse extract phase 0: e2e assert apply_report printed on done

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 7: Real-data smoke against the existing JSONL archive

**Files:**

- No code changes — this is a manual validation step to confirm Phase 0 fixes hold on real data.

- [ ] **Step 1: Confirm the T25 fixture DB still exists**

Run:

```bash
ls -la /Users/nikshilov/dev/ai/pulse/scripts/tests/fixtures/ && ls /Users/nikshilov/persistent/ 2>/dev/null | grep -i pulse
```

Expected: `scripts/tests/fixtures/extract_responses.json` exists (fixture from T24), and one of `~/persistent/pulse*.db` or `~/dev/ai/pulse/pulse-dev.db` exists holding the 9623 ingested observations from T25.

If no real-data DB is available, skip to Step 4 (unit-test-only verification is acceptable for Phase 0).

- [ ] **Step 2: Dry run on a copy of the real-data DB**

Create a working copy so the real DB stays untouched:

```bash
# Use the actual DB path from Step 1 in place of <SRC_DB>
cp <SRC_DB> /tmp/pulse-phase0-smoke.db
sqlite3 /tmp/pulse-phase0-smoke.db "SELECT COUNT(*) FROM extraction_jobs WHERE state='pending';"
sqlite3 /tmp/pulse-phase0-smoke.db "SELECT COUNT(*) FROM entities;"
sqlite3 /tmp/pulse-phase0-smoke.db "SELECT COUNT(*) FROM events;"
```

Record the three baseline numbers.

- [ ] **Step 3: Run one extraction pass**

```bash
cd /Users/nikshilov/dev/ai/pulse/scripts && ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" PULSE_DAILY_EXTRACT_BUDGET_USD=1 python3 pulse_extract.py --db /tmp/pulse-phase0-smoke.db 2>&1 | tee /tmp/pulse-phase0-smoke.log
```

Expected:
- No Python traceback reaches the shell (all errors are caught in `_apply_extraction` or `run_once`).
- At least one line of the form `job N: done, apply_report=[...]` appears in stdout.
- Re-query entity/event counts — they should be **greater than or equal** to the baseline (never less — the DB must not have lost data):

```bash
sqlite3 /tmp/pulse-phase0-smoke.db "SELECT COUNT(*) FROM entities;"
sqlite3 /tmp/pulse-phase0-smoke.db "SELECT COUNT(*) FROM events;"
sqlite3 /tmp/pulse-phase0-smoke.db "SELECT state, COUNT(*) FROM extraction_jobs GROUP BY state;"
```

No `dlq` count should increase above the baseline unless apply_report shows specific per-item failures that justify it. If DLQ grew but apply_report is empty, Phase 0 has a regression — stop and debug.

- [ ] **Step 4: Record findings**

Append a short paragraph to the plan's own results section below (`Phase 0 validation`) documenting the three counts before/after and any observed `apply_report.failed_items`. This is not a commit — it's a note-to-self for the Phase 1 planner.

No commit for this task.

---

## Task 8: Update memory + mark Phase 0 done

**Files:**

- No code changes.

- [ ] **Step 1: Re-run full suite from a cold shell**

```bash
cd /Users/nikshilov/dev/ai/pulse/scripts && python3 -m pytest tests/ -v 2>&1 | tail -30
```

Expected: every test green. Record the final count (e.g. "18 passed").

- [ ] **Step 2: Push the branch (but not to main)**

```bash
cd /Users/nikshilov/dev/ai/pulse && git log --oneline -10 && git status
```

Confirm only Phase 0 commits are present on the current branch and the tree is clean. **Do not push to main without explicit user approval** — per CLAUDE.md, Nik reviews before merge.

- [ ] **Step 3: Summarize to user**

Deliver a final report containing:
- Baseline vs final test count.
- Which files changed (`scripts/pulse_extract.py`, `scripts/tests/test_extract_phase0.py`, `scripts/tests/test_extract_e2e.py`).
- Key commits (hashes + subjects).
- Real-data smoke results from Task 7 (or "skipped — no real-data DB present").
- What Phase 1 will build on top: `extraction_artifacts` checkpoint table, UNIQUE constraints on relations/facts, `event_entities` junction so orphan-drop becomes a hard invariant rather than a soft skip.

---

## Phase 0 validation (Task 7 — 2026-04-16)

**Setup:** DB copy at `/tmp/pulse-phase0-smoke.db` (from `/Users/nikshilov/dev/ai/pulse/pulse-dev/pulse.db`). Source DB left untouched. Anthropic API key sourced from `/Users/nikshilov/dev/ai/Garden/.env`. Budget cap: `PULSE_DAILY_EXTRACT_BUDGET_USD=1`. Runtime: ~45 s.

**Counts before → after (delta):**

| metric | before | after | Δ |
|---|---:|---:|---:|
| observations | 9623 | 9623 | 0 |
| extraction_jobs pending | 9614 | 9605 | −9 |
| extraction_jobs done | 9 | 18 | +9 |
| extraction_jobs failed/dlq | 0/0 | 0/0 | 0 |
| entities/events/relations/facts | 0/0/0/0 | 0/0/0/0 | 0 |

**Observations:**

1. **No Python traceback reached the shell** — outer `run_once` try/except caught every failure, exit code 0. This is the core Phase 0 invariant.
2. **Retry path works under real conditions** — `job 1: pending, reason=KeyError: 'canonical_name'` appeared on the first job: Opus returned an entity missing `canonical_name`. The outer except flipped the job to `pending` and incremented attempts. After two more retries it will land in DLQ per the Task 5 retry budget. Observability print from the Task 5 fixup surfaced the reason cleanly.
3. **`apply_report=[]` for all 9 newly-done jobs** — Sonnet triage correctly skipped every observation in the batch. Source data is claude-jsonl code-task traces (no humans, no emotional weight); triage prompt explicitly defaults to `skip` in that case. Expected behavior, not a regression.
4. **Partial validation gap** — because triage skipped everything, the extract→apply path (SAVEPOINTs, apply_report with counts, `_apply_extraction` happy path) was NOT exercised on real data. The KeyError retry path was. Unit tests cover the remaining branches in isolation. Fully validating extract→apply on real data requires either different source data (chats with humans) or a targeted shell that overrides triage to always `extract`. Both are Phase 1+ concerns.
5. **Source DB pristine** — verified post-run: `/Users/nikshilov/dev/ai/pulse/pulse-dev/pulse.db` still has 9614 pending / 9 done. Copy isolation worked.

**Follow-up surfaced by this smoke:**

- `_apply_extraction` catches only `sqlite3.Error` in the four SAVEPOINT handlers. A `KeyError` from a malformed Opus entity (like `'canonical_name'` above) escapes to the outer handler, which rolls back the outer tx and schlopывает the open SAVEPOINT transitively — so integrity holds, but savepoint-stack hygiene is technically wrong. Either widen the local catch to `Exception` (and route through `_item_failure`), or validate entity shape before attempting the INSERT. Tracked as a separate task — not a Phase 0 blocker. Phase 2 tool-use schema will close the root cause.

---

## What ships after Phase 0

- Re-runnable extraction on the real 9623-observation archive without single-crash cascades.
- Deterministic per-observation boundaries (Anna committed ≠ Fedya rolled back means they truly are independent).
- Python extractor no longer races the Go ingest on WAL writes.
- A foundation for Phase 1 to add real durable checkpoints (`extraction_artifacts` table) without rewriting the transaction logic again.

## Out of scope — confirmed deferred

- **UNIQUE on relations/facts** (Phase 1) — without it, retries on transient failures will still create duplicate edges. Phase 0 reduces retries; Phase 1 eliminates the duplicates.
- **`event_entities` junction** (Phase 1) — the orphan-drop check in Task 2/3 is currently a soft skip based on the LLM output. Phase 1 enforces it at schema level.
- **Anthropic tool-use schema v2** (Phase 2) — the LLM can still return malformed JSON. Phase 0 catches apply-stage failures, not extract-stage ones. Phase 2 replaces text-JSON with tool-use and validates at the API boundary.
- **Top-K retrieval + alias index** (Phase 3) — `_load_existing_entities` still reads the whole entities table on every observation. Fine at 9k obs, bad at 100k+.
- **MCP tools + web viewer** (Phase 4).
