# Pulse Extraction Phase 1 — Design

**Date:** 2026-04-16
**Branch target:** new `extract-phase-1` off `main` after Phase 0 merge (or off `graph-populator-m1-m3` if Phase 0 still open)
**Scope:** Schema hardening + write-path UPSERT + checkpoint resumability + Phase 0 savepoint hygiene follow-up

## Context

Phase 0 (PR #1, `graph-populator-m1-m3`) shipped per-observation SAVEPOINT isolation and `apply_report`. It closed the #1 consensus bug from the v1 12-judge review (9/10 cited it) — one crash no longer rolls back an entire job.

Phase 1 closes the next tier of v1 consensus bugs that the v2 design (overall 7.40/10, 5 ship) incorporated:

| v1 bug (n judges) | Phase 1 fix |
|---|---|
| No UNIQUE on relations/facts (5/10) | UNIQUE indices + UPSERT writes |
| Orphan events — `entities_involved` not persisted (4/10) | `event_entities` junction table |
| Cost cliff on re-run after mid-job crash (GPT-5.4 Pro) | `extraction_artifacts` checkpoint table |
| KeyError escaping SAVEPOINT scope (Phase 0 follow-up #84) | Widen `except sqlite3.Error` to `except Exception` |

**Out of scope (deferred):**
- Phase 2: Anthropic tool-use schema v2 at API boundary (closes KeyError root cause; Phase 1 only treats symptom)
- Phase 3: `entity_aliases` table + top-K retrieval + unicode normalizer (resolver scaling)
- Phase 4: MCP tools + web viewer
- v3 candidates from v2 judge review: alias-learning gates, alias blocklist, scorer calibration (all `[1×]`, no consensus)

## Goals and non-goals

**Goals:**
1. Data-consistency invariant: duplicate `(from, to, kind)` relation or `(entity, text)` fact cannot exist in DB.
2. Referential invariant: every row in `event_entities` references a valid event and a valid entity. Events without any resolved entities are rejected pre-write (schema enforces what Phase 0 enforced procedurally).
3. Restart invariant: a job interrupted between Sonnet triage and Opus extract, or mid-extract across multiple observations, resumes from its last committed artifact — the LLM calls already paid for are not repeated.
4. Hygiene: malformed LLM responses that raise `KeyError` (or any non-sqlite exception) are contained by the per-item SAVEPOINT, reported in `failed_items`, and do not escape to the outer observation transaction.

**Non-goals:**
- Fuzzy-dedup of near-duplicate facts ("I love coffee" vs "i love coffee"). Phase 1 dedup is exact-string only.
- Entity dedup by canonical_name (resolver owns this; Phase 3 adds alias index).
- Concurrent-worker safety beyond what `PRAGMA busy_timeout=5000` from Phase 0 provides.
- Validating LLM responses before write (Phase 2 tool-use schema closes this).

## Architecture

Four independent layers, each introduced in its own commit sequence, integrated in a single PR:

1. **Schema** — migration `006_phase_1.sql` adds UNIQUE indices, `event_entities`, and `extraction_artifacts`. Non-breaking because:
   - No UNIQUE on existing tables violates current data (resolver never wrote duplicate relations/facts in practice — spot-check via `COUNT(*) GROUP BY from,to,kind HAVING COUNT>1` on `pulse-dev/pulse.db` during T2 test).
   - New tables are additive.
2. **Writes** — `_apply_extraction` switches to `INSERT ... ON CONFLICT` forms and writes to `event_entities`. Schema changes from layer 1 are required for this layer to compile.
3. **Checkpoint** — `run_once` reads/writes `extraction_artifacts`. Writes use the same per-operation transaction pattern as `_set_job_state` (own `BEGIN IMMEDIATE/COMMIT`, independent of apply tx).
4. **Hygiene** — `_apply_extraction` SAVEPOINT handlers widen `except sqlite3.Error` → `except Exception`, reason string includes exception class name.

Each layer is independently testable and bisectable. Tests land alongside the layer they exercise.

## Components

### Layer 1: Schema migration `006_phase_1.sql`

```sql
-- Non-breaking: resolver hasn't been writing duplicates (verified pre-migration).
CREATE UNIQUE INDEX idx_relations_unique ON relations(from_entity_id, to_entity_id, kind);
CREATE UNIQUE INDEX idx_facts_unique ON facts(entity_id, text);
-- Intentionally NO UNIQUE on entities: resolver handles canonical_name dedup;
-- legitimate same-name-different-kind ("Anna" person vs "Anna" place) stays permissible.

CREATE TABLE event_entities (
  event_id   INTEGER NOT NULL REFERENCES events(id)   ON DELETE CASCADE,
  entity_id  INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  PRIMARY KEY (event_id, entity_id)
);
CREATE INDEX idx_event_entities_entity ON event_entities(entity_id);

CREATE TABLE extraction_artifacts (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id       INTEGER NOT NULL REFERENCES extraction_jobs(id) ON DELETE CASCADE,
  kind         TEXT NOT NULL CHECK (kind IN ('triage','extract')),
  obs_id       TEXT,  -- NULL for kind='triage' (covers whole job), set for kind='extract' (per-obs)
  payload_json TEXT NOT NULL,
  model        TEXT NOT NULL,
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE (job_id, kind, obs_id)
);
CREATE INDEX idx_artifacts_job ON extraction_artifacts(job_id, kind);
```

**Rationale for each choice:**
- **`(job_id, kind, obs_id)` unique** — checkpoint is per-job per-stage per-observation; a given obs should have exactly one artifact for each kind. Prevents duplicate saves on retry.
- **`obs_id TEXT`** — observations have composite identity (`source_kind + source_id + version`); the job's `observation_ids` field is already serialized as a string list. Use the same id form here rather than introducing an INTEGER FK.
- **`ON DELETE CASCADE`** — erasure path (GDPR) deletes an entity; its junction rows and artifacts should go with it. Job deletion cleans up its artifacts.
- **`payload_json`** — raw LLM response (verdicts for triage, entities/events/relations/facts for extract). Keeping raw means Phase 2's tool-use schema migration can re-validate old artifacts without re-calling LLM.

### Layer 2: Writes layer — `_apply_extraction`

Current state (Phase 0): plain `INSERT` for entities/events/relations/facts, each wrapped in a SAVEPOINT so one failure is item-local.

**Changes:**

```python
# entities — unchanged (no UNIQUE; resolver owns dedup)
con.execute("INSERT INTO entities (canonical_name, kind, ...) VALUES (...)")

# events — plain INSERT (no UNIQUE), then junction writes
event_id = con.execute("INSERT INTO events (title, ...) VALUES (...)").lastrowid
for entity_name in event["entities_involved"]:
    entity_id = resolved_names[entity_name]  # resolver output, not LLM name
    con.execute(
        "INSERT OR IGNORE INTO event_entities(event_id, entity_id) VALUES (?, ?)",
        (event_id, entity_id),
    )

# relations — UPSERT on conflict
con.execute("""
    INSERT INTO relations(from_entity_id, to_entity_id, kind, strength, first_seen, last_seen)
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT(from_entity_id, to_entity_id, kind)
    DO UPDATE SET
        strength = strength + 1,
        last_seen = excluded.last_seen
""", (...))

# facts — INSERT OR IGNORE on conflict
con.execute("""
    INSERT INTO facts(entity_id, text, confidence, scorer_version, created_at)
    VALUES (?, ?, ?, ?, ?)
    ON CONFLICT(entity_id, text) DO NOTHING
""", (...))
```

**Orphan-drop invariant** (from Phase 0): event without any resolvable `entities_involved` goes to `failed_items` pre-SAVEPOINT with reason `"events require entities_involved"`. Unchanged — still enforced procedurally before junction writes begin.

**Partial resolution:** event with 3 names, resolver finds 2 → event is written, junction gets 2 rows, 1 unresolved name ignored. (If stricter "all-or-nothing" is needed, move the drop into the SAVEPOINT; Phase 1 chooses permissive.)

**`apply_report` shape extension:** new top-level field `event_entities_written: int` (count of junction rows inserted). Keeps backward compatibility with existing keys.

### Layer 3: Checkpoint layer — `run_once`

Two new helpers:

```python
def _get_artifact(con, job_id: int, kind: str, obs_id: str | None) -> dict | None:
    """Returns parsed payload_json or None."""
    row = con.execute(
        "SELECT payload_json FROM extraction_artifacts WHERE job_id=? AND kind=? AND obs_id IS ?",
        (job_id, kind, obs_id),
    ).fetchone()
    return json.loads(row[0]) if row else None

def _save_artifact(con, job_id: int, kind: str, obs_id: str | None,
                   payload: dict, model: str) -> None:
    """Own BEGIN IMMEDIATE/COMMIT, independent of apply tx."""
    con.execute("BEGIN IMMEDIATE")
    con.execute(
        "INSERT OR REPLACE INTO extraction_artifacts(job_id, kind, obs_id, payload_json, model) "
        "VALUES (?, ?, ?, ?, ?)",
        (job_id, kind, obs_id, json.dumps(payload), model),
    )
    con.execute("COMMIT")
```

`run_once` flow change:

```python
# Triage stage
verdicts = _get_artifact(con, job_id, "triage", None)
if verdicts is None:
    verdicts = call_sonnet_triage(obs_batch)
    _save_artifact(con, job_id, "triage", None, verdicts, TRIAGE_MODEL)

# Extract stage (per obs)
for obs_id, verdict in zip(obs_ids, verdicts):
    if verdict["verdict"] != "extract":
        continue
    result = _get_artifact(con, job_id, "extract", obs_id)
    if result is None:
        result = call_opus_extract(obs)
        _save_artifact(con, job_id, "extract", obs_id, result, EXTRACT_MODEL)

    con.execute("BEGIN IMMEDIATE")
    try:
        report = _apply_extraction(con, obs_id, result)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
```

**Idempotency on replay:**
- Same triage artifact → same verdicts → same extract calls skipped from artifact → same results → `_apply_extraction` is idempotent because of UNIQUE+UPSERT+`INSERT OR IGNORE` from Layer 2.
- Crash after `_apply_extraction` commit but before `_set_job_state(done)` commit: next run re-plays apply; UPSERT bumps `strength` by 1 too many for relations touched in that obs. **This is the known imperfection** — the fix requires co-locating apply and state transition in one tx, which Phase 0 specifically separated for monotonic-attempts invariant. Tradeoff documented; Phase 2 tool-use + idempotency-key pattern can close it. Not blocking.

### Layer 4: Hygiene (task #84)

Replace each SAVEPOINT catch site in `_apply_extraction`:

```python
# before
except sqlite3.Error as e:

# after
except Exception as e:
    failed_items.append({
        "item_kind": "entity",  # or event/relation/fact
        "reason": f"{type(e).__name__}: {e}",
        "detail": {"index": idx},
    })
```

Four call sites (entity / event / relation / fact). Pre-check paths (orphan-drop, missing canonical_name) stay the same — they handle validation before SAVEPOINT opens.

**What this catches:** `KeyError: 'canonical_name'` from malformed Opus entity dict (observed in Phase 0 real-data smoke), any other LLM-shape mismatch, any unexpected non-DB error. Widens the safety net without changing normal-path behavior.

## Data flow

```
┌──────────────┐
│ extraction_  │  run_once picks pending job
│ jobs.pending │
└──────┬───────┘
       │
       ▼
  ┌─────────────────────────────────────────┐
  │ Check artifact(job, 'triage', NULL)     │
  │   exists? use it.                       │
  │   missing? call Sonnet, save artifact.  │
  └──────┬──────────────────────────────────┘
         │ verdicts
         ▼
  For each obs in verdicts[verdict='extract']:
  ┌─────────────────────────────────────────┐
  │ Check artifact(job, 'extract', obs_id)  │
  │   exists? use it.                       │
  │   missing? call Opus, save artifact.    │
  └──────┬──────────────────────────────────┘
         │ result
         ▼
  BEGIN IMMEDIATE (per-obs tx)
    _apply_extraction:
      entities  → INSERT (SAVEPOINT per item)
      events    → INSERT (SAVEPOINT per item)
                  → event_entities INSERT OR IGNORE
      relations → UPSERT (SAVEPOINT per item)
      facts     → INSERT OR IGNORE (SAVEPOINT per item)
  COMMIT / ROLLBACK

  [independent tx] _set_job_state(done | pending | dlq)
```

## Error handling

| Failure | Containment | Recovery |
|---|---|---|
| Malformed LLM entity (KeyError) | SAVEPOINT + Layer 4 widened except | `failed_items[]`, rest of obs commits |
| Duplicate relation/fact | UPSERT/IGNORE no-op | Silent (expected); strength bumps for relation |
| Crash mid-triage | No artifact written | Next run: re-triage |
| Crash mid-extract (after obs 3/10 done) | Artifacts 1-3 saved | Next run: obs 1-3 skipped, 4-10 extracted |
| Crash between apply-commit and state-commit | Apply committed | Next run: re-apply (idempotent except for relation strength over-count by 1) |
| `sqlite3.IntegrityError` on unexpected UNIQUE violation | SAVEPOINT rollback (Layer 4 catches Exception) | `failed_items[]` |

## Testing

Tests in new file `scripts/tests/test_extract_phase1.py`:

**Layer 1 (schema):**
- `test_relations_unique_constraint_enforces_single_tuple`
- `test_facts_unique_constraint_enforces_single_tuple`
- `test_event_entities_fk_cascades_on_entity_delete`
- `test_extraction_artifacts_unique_job_kind_obs`

**Layer 2 (writes):**
- `test_relation_upsert_bumps_strength_and_last_seen`
- `test_fact_insert_or_ignore_is_noop_on_dup`
- `test_event_entities_junction_written_for_each_resolved_entity`
- `test_event_with_partial_resolution_writes_only_resolved`
- `test_apply_report_includes_event_entities_written`

**Layer 3 (checkpoint):**
- `test_triage_artifact_saved_after_sonnet_call`
- `test_extract_artifact_saved_after_opus_call_per_obs`
- `test_restart_reuses_triage_artifact_no_sonnet_call`
- `test_restart_reuses_extract_artifact_per_obs_no_opus_call`
- `test_save_artifact_is_own_transaction`

**Layer 4 (hygiene):**
- `test_keyerror_in_entity_caught_by_savepoint`
- `test_keyerror_reason_includes_exception_class_name`

Existing Phase 0 tests (`test_extract_phase0.py`, 6 tests) should continue passing unmodified — Phase 1 layers are additive to Phase 0 invariants.

E2E test `test_extract_e2e.py::test_e2e_prints_apply_report` updated to assert new `event_entities_written` key.

## Migration strategy

1. Pre-migration check: run `SELECT from_entity_id, to_entity_id, kind, COUNT(*) FROM relations GROUP BY 1,2,3 HAVING COUNT>1` (and equivalent for facts) on a copy of `pulse-dev/pulse.db`. If either returns a row, abort — needs a data-cleanup step first. Empty result confirms safe migration.
2. Run `006_phase_1.sql` via existing migration runner.
3. Post-migration: schema tests validate structure, then writes-layer commits become safe to deploy.

Schema is backward-compatible with currently-deployed writer: Phase 0 plain INSERTs still work on the new schema (UNIQUE only constrains new duplicates; Phase 0 code hasn't been producing duplicates). Writer switch to UPSERT is a separate commit that can land after schema validates.

## Open questions (resolved before plan phase)

- **Q: Should entities get UNIQUE(canonical_name, kind)?** A: No. Resolver handles canonical_name dedup and Phase 3 adds alias-index retrieval. A schema-level UNIQUE here would backfire on legitimate same-name-different-kind entities and on any resolver race.
- **Q: Should facts dedup be fuzzy (normalized hash)?** A: No for Phase 1. Exact-string UNIQUE catches the obvious bulk. Fuzzy dedup is Phase 3 resolver work.
- **Q: Should apply and state-transition be one tx to avoid relation-strength over-count on crash-replay?** A: No. Phase 0 separated them specifically to keep `attempts` monotonic and DLQ reachable when apply fails. The over-count is rare (only on crash mid-transition) and bounded (+1 per crash per relation). Acceptable; Phase 2 idempotency-keys close this cleanly.
- **Q: Should Layer 4 catch Exception or BaseException?** A: Exception. `BaseException` includes KeyboardInterrupt/SystemExit; we want those to propagate.

## Follow-ups surfaced for Phase 2+

- KeyError root cause → Anthropic tool-use schema v2 at API boundary (Phase 2)
- Relation-strength over-count on crash replay → idempotency-key or apply+state co-tx redesign (Phase 2)
- Entity dedup by canonical_name + aliases → `entity_aliases` table + top-K retrieval (Phase 3)
- Fuzzy fact dedup → Phase 3 resolver work

## Success criteria

1. Schema migration `006_phase_1.sql` runs cleanly against `pulse-dev/pulse.db` copy with zero data loss.
2. Full test suite: 38/1 (Phase 0) + new Phase 1 tests, all passing.
3. `_apply_extraction` applied twice to same extraction result produces identical DB state (modulo relation-strength bump; idempotent otherwise).
4. `run_once` interrupted mid-extract and restarted does not re-call Sonnet or re-call Opus for any obs already in artifacts.
5. `apply_report` carries `event_entities_written` count.
6. Malformed LLM entity (KeyError) appears in `failed_items[]` instead of escaping to outer transaction.
