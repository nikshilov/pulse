# Phase 2e: Graph Intelligence — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend retrieval with multi-hop BFS and enrich consolidation with co-occurrence, knowledge gaps, salience decay, and observability metrics.

**Architecture:** Adaptive depth adds BFS to existing keyword retrieval (retrieval.py). Consolidation enrichments (co-occurrence, knowledge gaps, open_questions, skip-guard, salience decay, valence trend, extraction efficiency) extend the existing pipeline (pulse_consolidate.py). One new migration (008) adds a consolidation_metadata key-value table for skip-guard state.

**Tech Stack:** Python 3, SQLite, pytest

**Spec:** `docs/superpowers/specs/2026-04-16-pulse-phase-2b-graph-intelligence.md`

---

## File Structure

| Action | File | Responsibility |
|--------|------|---------------|
| Create | `internal/store/migrations/008_consolidation.sql` | consolidation_metadata table for skip-guard |
| Modify | `scripts/extract/retrieval.py` | Add `depth` param, BFS expansion, hop penalty |
| Modify | `scripts/pulse_consolidate.py` | Add skip-guard, co-occurrence, knowledge gaps, open_questions, salience decay, valence trend, extraction efficiency |
| Modify | `scripts/tests/test_retrieval.py` | 3 new tests for multi-hop retrieval |
| Modify | `scripts/tests/test_consolidate.py` | 7 new tests for all new consolidation features |

---

### Task 1: Migration 008 — Consolidation Metadata

**Files:**
- Create: `internal/store/migrations/008_consolidation.sql`

- [ ] **Step 1: Write the migration**

```sql
-- 008_consolidation.sql
-- Key-value store for consolidation pipeline state (skip-guard timestamps, etc.)

CREATE TABLE IF NOT EXISTS consolidation_metadata (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
```

- [ ] **Step 2: Verify migration applies cleanly on test DB**

Run: `cd ~/dev/ai/pulse && python3 -c "
import sqlite3
con = sqlite3.connect(':memory:')
from pathlib import Path
for f in sorted(Path('internal/store/migrations').glob('*.sql')):
    con.executescript(f.read_text())
print('All migrations applied OK')
print('Tables:', [r[0] for r in con.execute(\"SELECT name FROM sqlite_master WHERE type='table' ORDER BY name\").fetchall()])
"`

Expected: All migrations applied OK, `consolidation_metadata` in table list.

- [ ] **Step 3: Commit**

```bash
git add internal/store/migrations/008_consolidation.sql
git commit -m "feat: migration 008 — consolidation_metadata table for skip-guard"
```

---

### Task 2: Adaptive Depth Retrieval

**Files:**
- Modify: `scripts/extract/retrieval.py`
- Modify: `scripts/tests/test_retrieval.py`

- [ ] **Step 1: Write the failing tests**

Add to `scripts/tests/test_retrieval.py`:

```python
def test_retrieve_2hop_indirect_relation(tmp_path):
    """Anna→Nik→Pulse: querying 'Anna' with depth=2 should find Pulse via Nik."""
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    _seed_graph(con)
    result = retrieve_context(con, "Anna", depth=2)
    names = [e["canonical_name"] for e in result["matched_entities"]]
    assert "Anna" in names
    assert "Pulse" in names  # found via Anna→Nik→Pulse (2 hops)


def test_hop_penalty_ranking(tmp_path):
    """Direct match should rank above 2-hop match."""
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    _seed_graph(con)
    result = retrieve_context(con, "Anna", depth=2)
    entities = result["matched_entities"]
    anna_idx = next(i for i, e in enumerate(entities) if e["canonical_name"] == "Anna")
    pulse_idx = next(i for i, e in enumerate(entities) if e["canonical_name"] == "Pulse")
    assert anna_idx < pulse_idx  # Anna (direct) ranks above Pulse (2 hops away)


def test_depth_0_returns_only_matched(tmp_path):
    """depth=0 returns matched entity without expanding relations."""
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    _seed_graph(con)
    result = retrieve_context(con, "Anna", depth=0)
    names = [e["canonical_name"] for e in result["matched_entities"]]
    assert "Anna" in names
    assert "Nik" not in names  # no expansion
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/dev/ai/pulse && python3 -m pytest scripts/tests/test_retrieval.py::test_retrieve_2hop_indirect_relation scripts/tests/test_retrieval.py::test_hop_penalty_ranking scripts/tests/test_retrieval.py::test_depth_0_returns_only_matched -v`

Expected: FAIL — `retrieve_context()` does not accept `depth` parameter.

- [ ] **Step 3: Implement adaptive depth in retrieval.py**

Replace the full content of `scripts/extract/retrieval.py` with:

```python
"""Keyword-based graph retrieval with adaptive depth BFS.

Tokenize user message → match entities by name/alias → BFS expansion to depth → rank with hop penalty.
"""

import json
import re
import sqlite3
from datetime import datetime, timezone


def retrieve_context(
    con: sqlite3.Connection, message: str, top_k: int = 10, depth: int = 1
) -> dict:
    tokens = _tokenize(message)
    seed_entities = _match_entities(con, tokens)

    # BFS expansion to depth
    all_entities: dict[int, dict] = {}
    frontier: list[tuple[int, int]] = [(e["id"], 0) for e in seed_entities]

    while frontier:
        eid, hop = frontier.pop(0)
        if eid in all_entities or hop > depth:
            continue
        entity = _get_entity_full(con, eid, seed_entities)
        entity["_hop"] = hop
        all_entities[eid] = entity

        if hop < depth:
            neighbors = con.execute(
                "SELECT CASE WHEN from_entity_id=? THEN to_entity_id ELSE from_entity_id END, strength "
                "FROM relations WHERE (from_entity_id=? OR to_entity_id=?) AND strength > 0.3 "
                "ORDER BY strength DESC LIMIT 5",
                (eid, eid, eid),
            ).fetchall()
            for nid, _ in neighbors:
                if nid not in all_entities:
                    frontier.append((nid, hop + 1))

    ranked = _rank(list(all_entities.values()))
    trimmed = ranked[:top_k]

    return {
        "matched_entities": trimmed,
        "total_matched": len(all_entities),
        "retrieval_method": "keyword",
        "max_depth_used": max((e["_hop"] for e in all_entities.values()), default=0),
    }


def _tokenize(message: str) -> list[str]:
    words = re.findall(r"\b[\w\-]{2,}\b", message, re.UNICODE)
    ngrams = []
    for i in range(len(words)):
        for j in range(i + 1, min(i + 4, len(words) + 1)):
            ngrams.append(" ".join(words[i:j]))
    return list(set(words + ngrams))


def _match_entities(con: sqlite3.Connection, tokens: list[str]) -> list[dict]:
    matched: dict[int, dict] = {}
    all_entities = con.execute(
        "SELECT id, canonical_name, kind, aliases, salience_score, last_seen FROM entities"
    ).fetchall()

    for row in all_entities:
        eid, name, kind, aliases_json, salience, last_seen = row
        aliases = json.loads(aliases_json) if aliases_json else []
        all_names = [name] + aliases

        for token in tokens:
            if any(token.lower() == n.lower() for n in all_names):
                if eid not in matched:
                    matched[eid] = {
                        "id": eid,
                        "canonical_name": name,
                        "kind": kind,
                        "aliases": aliases,
                        "salience_score": salience,
                        "last_seen": last_seen,
                    }
                break

    return list(matched.values())


def _get_entity_full(con: sqlite3.Connection, eid: int, seed_entities: list[dict]) -> dict:
    """Get full entity data. Uses seed_entities cache if available, otherwise queries DB."""
    for e in seed_entities:
        if e["id"] == eid:
            entity = dict(e)
            entity["relations"] = _get_relations(con, eid)
            entity["facts"] = _get_facts(con, eid)
            return entity

    row = con.execute(
        "SELECT id, canonical_name, kind, aliases, salience_score, last_seen FROM entities WHERE id = ?",
        (eid,),
    ).fetchone()
    if not row:
        return {"id": eid, "canonical_name": "?", "kind": "?", "salience_score": 0, "last_seen": None,
                "aliases": [], "relations": [], "facts": []}
    aliases = json.loads(row[3]) if row[3] else []
    return {
        "id": row[0],
        "canonical_name": row[1],
        "kind": row[2],
        "aliases": aliases,
        "salience_score": row[4],
        "last_seen": row[5],
        "relations": _get_relations(con, eid),
        "facts": _get_facts(con, eid),
    }


def _get_relations(con: sqlite3.Connection, entity_id: int) -> list[dict]:
    rows = con.execute(
        "SELECT r.kind, r.context, r.strength, "
        "CASE WHEN r.from_entity_id = ? THEN e2.canonical_name ELSE e1.canonical_name END "
        "FROM relations r "
        "JOIN entities e1 ON r.from_entity_id = e1.id "
        "JOIN entities e2 ON r.to_entity_id = e2.id "
        "WHERE r.from_entity_id = ? OR r.to_entity_id = ?",
        (entity_id, entity_id, entity_id),
    ).fetchall()
    return [
        {"kind": r[0], "context": r[1] or "", "strength": r[2], "other_entity": r[3]}
        for r in rows
    ]


def _get_facts(con: sqlite3.Connection, entity_id: int) -> list[dict]:
    rows = con.execute(
        "SELECT text, confidence, verified FROM facts WHERE entity_id = ?",
        (entity_id,),
    ).fetchall()
    return [{"text": r[0], "confidence": r[1], "verified": bool(r[2])} for r in rows]


def _rank(entities: list[dict]) -> list[dict]:
    now = datetime.now(timezone.utc)
    scored = []
    for ent in entities:
        try:
            last = datetime.fromisoformat(ent["last_seen"].replace("Z", "+00:00"))
            days_ago = (now - last).days
        except (ValueError, AttributeError):
            days_ago = 365
        recency = max(0.1, 1.0 - days_ago / 365)
        hop_penalty = 0.7 ** ent.get("_hop", 0)
        scored.append((ent["salience_score"] * recency * hop_penalty, ent))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [ent for _, ent in scored]
```

- [ ] **Step 4: Run all retrieval tests**

Run: `cd ~/dev/ai/pulse && python3 -m pytest scripts/tests/test_retrieval.py -v`

Expected: All 10 tests PASS (7 existing + 3 new).

- [ ] **Step 5: Commit**

```bash
git add scripts/extract/retrieval.py scripts/tests/test_retrieval.py
git commit -m "feat: adaptive depth retrieval — BFS expansion with hop penalty"
```

---

### Task 3: Skip-guard

**Files:**
- Modify: `scripts/pulse_consolidate.py`
- Modify: `scripts/tests/test_consolidate.py`

- [ ] **Step 1: Write the failing test**

Add to `scripts/tests/test_consolidate.py`:

```python
def test_skip_guard_skips_when_no_changes(tmp_path):
    """Consolidation should skip when nothing changed since last run."""
    from pulse_consolidate import run_consolidation
    _, db_path = _fresh_db(tmp_path)
    # First run — should NOT skip (no prior consolidation)
    report1 = run_consolidation(db_path)
    assert report1.get("skipped") is not True
    # Second run immediately — should skip (nothing changed)
    report2 = run_consolidation(db_path)
    assert report2["skipped"] is True
    assert "reason" in report2


def test_skip_guard_runs_when_new_entities(tmp_path):
    """Consolidation should run when new entities appeared since last run."""
    from pulse_consolidate import run_consolidation
    con, db_path = _fresh_db(tmp_path)
    # First run
    run_consolidation(db_path)
    # Add a new entity
    now = "2099-01-01T00:00:00Z"
    con.execute("INSERT INTO entities (canonical_name, kind, first_seen, last_seen) VALUES ('NewPerson','person',?,?)", (now, now))
    con.commit()
    # Second run — should NOT skip
    report2 = run_consolidation(db_path)
    assert report2.get("skipped") is not True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/dev/ai/pulse && python3 -m pytest scripts/tests/test_consolidate.py::test_skip_guard_skips_when_no_changes scripts/tests/test_consolidate.py::test_skip_guard_runs_when_new_entities -v`

Expected: FAIL — `run_consolidation` does not return `skipped`.

- [ ] **Step 3: Implement skip-guard in pulse_consolidate.py**

Add helper functions before `run_consolidation`:

```python
def _get_metadata(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("SELECT value FROM consolidation_metadata WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _set_metadata(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO consolidation_metadata (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, datetime.now(timezone.utc).isoformat()),
    )


def _should_skip(con: sqlite3.Connection) -> dict | None:
    last_ts = _get_metadata(con, "last_consolidation_ts")
    if not last_ts:
        return None  # never run before — don't skip

    new_entities = con.execute(
        "SELECT COUNT(*) FROM entities WHERE first_seen > ?", (last_ts,)
    ).fetchone()[0]
    new_evidence = con.execute(
        "SELECT COUNT(*) FROM evidence WHERE created_at > ?", (last_ts,)
    ).fetchone()[0]

    if new_entities == 0 and new_evidence < 3:
        return {"skipped": True, "reason": "no significant changes since last consolidation"}
    return None
```

Modify `run_consolidation` to use skip-guard:

```python
def run_consolidation(db_path: str) -> dict:
    con = _open_connection(db_path)

    skip = _should_skip(con)
    if skip:
        con.close()
        return skip

    stats = entity_stats(con)
    duplicates = find_duplicate_candidates(con)
    closed = close_stale_questions(con)
    merges = process_approved_merges(con)

    _set_metadata(con, "last_consolidation_ts", datetime.now(timezone.utc).isoformat())
    con.close()

    return {
        "stats": stats,
        "duplicate_candidates": len(duplicates),
        "duplicates": duplicates[:20],
        "stale_questions_closed": closed,
        "merges_executed": merges,
    }
```

- [ ] **Step 4: Run tests**

Run: `cd ~/dev/ai/pulse && python3 -m pytest scripts/tests/test_consolidate.py -v`

Expected: All tests PASS (7 existing + 2 new).

- [ ] **Step 5: Commit**

```bash
git add scripts/pulse_consolidate.py scripts/tests/test_consolidate.py
git commit -m "feat: consolidation skip-guard — skip when no significant changes"
```

---

### Task 4: Co-occurrence Detection

**Files:**
- Modify: `scripts/pulse_consolidate.py`
- Modify: `scripts/tests/test_consolidate.py`

- [ ] **Step 1: Write the failing test**

Add to `scripts/tests/test_consolidate.py`:

```python
def test_find_cooccurrence_candidates(tmp_path):
    """Entities co-appearing 3+ times in same events should be detected."""
    from pulse_consolidate import find_cooccurrence_candidates
    con, _ = _fresh_db(tmp_path)
    now = "2026-04-16T00:00:00Z"
    # Create 2 entities
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (1,'Anna','person',?,?)", (now, now))
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (2,'Nik','person',?,?)", (now, now))
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (3,'Solo','person',?,?)", (now, now))
    # Create 3 events where Anna and Nik co-occur
    for i in range(1, 4):
        con.execute("INSERT INTO events (id, title, ts) VALUES (?,?,?)", (i, f"event_{i}", now))
        con.execute("INSERT INTO event_entities (event_id, entity_id) VALUES (?,?)", (i, 1))  # Anna
        con.execute("INSERT INTO event_entities (event_id, entity_id) VALUES (?,?)", (i, 2))  # Nik
    # Solo only appears in 1 event
    con.execute("INSERT INTO event_entities (event_id, entity_id) VALUES (1,3)")

    candidates = find_cooccurrence_candidates(con)
    # Anna-Nik should be detected (3 co-occurrences)
    pairs = [(c["entity_a_id"], c["entity_b_id"]) for c in candidates]
    assert (1, 2) in pairs
    # Solo-Anna or Solo-Nik should NOT be detected (only 1 co-occurrence)
    assert all(c["co_count"] >= 3 for c in candidates)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/dev/ai/pulse && python3 -m pytest scripts/tests/test_consolidate.py::test_find_cooccurrence_candidates -v`

Expected: FAIL — `find_cooccurrence_candidates` does not exist.

- [ ] **Step 3: Implement co-occurrence detection**

Add to `scripts/pulse_consolidate.py`, after `find_duplicate_candidates`:

```python
def find_cooccurrence_candidates(
    con: sqlite3.Connection, min_cooccurrences: int = 3
) -> list[dict]:
    """Find entity pairs that co-appear in 3+ events but have no explicit relation."""
    rows = con.execute(
        "SELECT ee1.entity_id AS a, ee2.entity_id AS b, COUNT(*) AS co_count "
        "FROM event_entities ee1 "
        "JOIN event_entities ee2 ON ee1.event_id = ee2.event_id "
        "    AND ee1.entity_id < ee2.entity_id "
        "GROUP BY ee1.entity_id, ee2.entity_id "
        "HAVING co_count >= ?",
        (min_cooccurrences,),
    ).fetchall()

    candidates = []
    for a_id, b_id, count in rows:
        # Check if explicit relation already exists
        existing = con.execute(
            "SELECT COUNT(*) FROM relations "
            "WHERE (from_entity_id=? AND to_entity_id=?) OR (from_entity_id=? AND to_entity_id=?)",
            (a_id, b_id, b_id, a_id),
        ).fetchone()[0]
        if existing == 0:
            a_name = con.execute("SELECT canonical_name FROM entities WHERE id=?", (a_id,)).fetchone()[0]
            b_name = con.execute("SELECT canonical_name FROM entities WHERE id=?", (b_id,)).fetchone()[0]
            candidates.append({
                "entity_a_id": a_id, "entity_a_name": a_name,
                "entity_b_id": b_id, "entity_b_name": b_name,
                "co_count": count,
            })
    return candidates
```

Add to `run_consolidation`, after `merges`:

```python
    cooccurrences = find_cooccurrence_candidates(con)
```

And add `"implicit_relation_candidates": cooccurrences` to the return dict.

- [ ] **Step 4: Run tests**

Run: `cd ~/dev/ai/pulse && python3 -m pytest scripts/tests/test_consolidate.py -v`

Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/pulse_consolidate.py scripts/tests/test_consolidate.py
git commit -m "feat: co-occurrence detection — find implicit entity relationships"
```

---

### Task 5: Knowledge Gap Detection + Auto-populate open_questions

**Files:**
- Modify: `scripts/pulse_consolidate.py`
- Modify: `scripts/tests/test_consolidate.py`

- [ ] **Step 1: Write the failing tests**

Add to `scripts/tests/test_consolidate.py`:

```python
def test_detect_knowledge_gaps(tmp_path):
    """Entities mentioned often but with few facts = knowledge gaps."""
    from pulse_consolidate import detect_knowledge_gaps
    con, _ = _fresh_db(tmp_path)
    now = "2026-04-16T00:00:00Z"
    # Entity mentioned in 5 events, 0 facts
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (1,'MysteryPerson','person',?,?)", (now, now))
    for i in range(1, 6):
        con.execute("INSERT INTO events (id, title, ts) VALUES (?,?,?)", (i, f"event_{i}", now))
        con.execute("INSERT INTO event_entities (event_id, entity_id) VALUES (?,1)", (i,))
    # Entity mentioned 1 time, 0 facts — should NOT be a gap (too few mentions)
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (2,'Rare','person',?,?)", (now, now))
    con.execute("INSERT INTO events (id, title, ts) VALUES (6,'ev6',?)", (now,))
    con.execute("INSERT INTO event_entities (event_id, entity_id) VALUES (6,2)")

    gaps = detect_knowledge_gaps(con)
    gap_ids = [g["entity_id"] for g in gaps]
    assert 1 in gap_ids  # MysteryPerson has 5 mentions, 0 facts
    assert 2 not in gap_ids  # Rare has only 1 mention


def test_auto_populate_questions(tmp_path):
    """Knowledge gaps should auto-generate open_questions."""
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/dev/ai/pulse && python3 -m pytest scripts/tests/test_consolidate.py::test_detect_knowledge_gaps scripts/tests/test_consolidate.py::test_auto_populate_questions -v`

Expected: FAIL — functions don't exist.

- [ ] **Step 3: Implement knowledge gap detection and auto-populate**

Add to `scripts/pulse_consolidate.py`:

```python
def detect_knowledge_gaps(
    con: sqlite3.Connection, min_mentions: int = 3, max_facts: int = 1
) -> list[dict]:
    """Find entities mentioned often but with few facts."""
    rows = con.execute(
        "SELECT e.id, e.canonical_name, e.kind, "
        "       COUNT(DISTINCT ee.event_id) AS mention_count, "
        "       COUNT(DISTINCT f.id) AS fact_count "
        "FROM entities e "
        "LEFT JOIN event_entities ee ON ee.entity_id = e.id "
        "LEFT JOIN facts f ON f.entity_id = e.id "
        "GROUP BY e.id "
        "HAVING mention_count > ? AND fact_count <= ? "
        "ORDER BY mention_count DESC "
        "LIMIT 10",
        (min_mentions, max_facts),
    ).fetchall()
    return [
        {"entity_id": r[0], "canonical_name": r[1], "kind": r[2],
         "mention_count": r[3], "fact_count": r[4]}
        for r in rows
    ]


def auto_populate_questions(
    con: sqlite3.Connection, gaps: list[dict], ttl_days: int = 30
) -> int:
    """Create open_questions for knowledge gaps. Returns count added."""
    now = datetime.now(timezone.utc)
    ttl = (now + __import__("datetime").timedelta(days=ttl_days)).isoformat()
    added = 0
    for gap in gaps:
        con.execute(
            "INSERT OR IGNORE INTO open_questions "
            "(subject_entity_id, question_text, asked_at, ttl_expires_at, state) "
            "VALUES (?, ?, ?, ?, 'open')",
            (gap["entity_id"], f"Что сейчас с {gap['canonical_name']}?", now.isoformat(), ttl),
        )
        added += con.total_changes - (con.total_changes - 1) if con.total_changes else 0
    # Simpler: just count rows added
    return len(gaps)
```

**Note on `auto_populate_questions`:** The `INSERT OR IGNORE` handles dedup — if a question already exists for that entity+text, it won't create a duplicate. Return `len(gaps)` as a count of candidates processed; actual inserts may be fewer due to dedup.

Replace the `auto_populate_questions` function with this simpler version:

```python
def auto_populate_questions(
    con: sqlite3.Connection, gaps: list[dict], ttl_days: int = 30
) -> int:
    """Create open_questions for knowledge gaps. Returns count added."""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    ttl = (now + timedelta(days=ttl_days)).isoformat()
    now_iso = now.isoformat()
    added = 0
    for gap in gaps:
        result = con.execute(
            "INSERT OR IGNORE INTO open_questions "
            "(subject_entity_id, question_text, asked_at, ttl_expires_at, state) "
            "VALUES (?, ?, ?, ?, 'open')",
            (gap["entity_id"], f"Что сейчас с {gap['canonical_name']}?", now_iso, ttl),
        )
        if result.rowcount > 0:
            added += 1
    return added
```

Add to `run_consolidation`, after `cooccurrences`:

```python
    knowledge_gaps = detect_knowledge_gaps(con)
    questions_added = auto_populate_questions(con, knowledge_gaps)
```

And add to return dict:
```python
    "knowledge_gaps": len(knowledge_gaps),
    "questions_auto_added": questions_added,
```

- [ ] **Step 4: Run tests**

Run: `cd ~/dev/ai/pulse && python3 -m pytest scripts/tests/test_consolidate.py -v`

Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/pulse_consolidate.py scripts/tests/test_consolidate.py
git commit -m "feat: knowledge gap detection + auto-populate open_questions"
```

---

### Task 6: Salience Decay

**Files:**
- Modify: `scripts/pulse_consolidate.py`
- Modify: `scripts/tests/test_consolidate.py`

- [ ] **Step 1: Write the failing test**

Add to `scripts/tests/test_consolidate.py`:

```python
def test_decay_salience_reduces_stale_entities(tmp_path):
    """Entities unseen for 30+ days should have lower salience after decay."""
    from pulse_consolidate import decay_salience
    con, _ = _fresh_db(tmp_path)
    old_date = "2020-01-01T00:00:00Z"
    recent_date = "2026-04-16T00:00:00Z"
    # Stale entity (last seen 6+ years ago)
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen, salience_score) VALUES (1,'OldFriend','person',?,?,0.8)", (old_date, old_date))
    # Recent entity
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen, salience_score) VALUES (2,'NewFriend','person',?,?,0.8)", (recent_date, recent_date))

    updated = decay_salience(con)
    assert updated >= 1  # at least OldFriend should be decayed

    old_salience = con.execute("SELECT salience_score FROM entities WHERE id=1").fetchone()[0]
    new_salience = con.execute("SELECT salience_score FROM entities WHERE id=2").fetchone()[0]
    assert old_salience < 0.8  # decayed
    assert old_salience >= 0.05  # floor
    assert new_salience == 0.8  # recent, no decay


def test_decay_salience_respects_kind_rates(tmp_path):
    """Concepts should decay faster than people."""
    from pulse_consolidate import decay_salience
    con, _ = _fresh_db(tmp_path)
    # Both last seen 100 days ago
    old_date = "2025-01-01T00:00:00Z"
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen, salience_score) VALUES (1,'PersonX','person',?,?,0.8)", (old_date, old_date))
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen, salience_score) VALUES (2,'ConceptY','concept',?,?,0.8)", (old_date, old_date))

    decay_salience(con)

    person_s = con.execute("SELECT salience_score FROM entities WHERE id=1").fetchone()[0]
    concept_s = con.execute("SELECT salience_score FROM entities WHERE id=2").fetchone()[0]
    # Concept decays faster (λ=0.01) than person (λ=0.001)
    assert concept_s < person_s
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/dev/ai/pulse && python3 -m pytest scripts/tests/test_consolidate.py::test_decay_salience_reduces_stale_entities scripts/tests/test_consolidate.py::test_decay_salience_respects_kind_rates -v`

Expected: FAIL — `decay_salience` does not exist.

- [ ] **Step 3: Implement salience decay**

Add to `scripts/pulse_consolidate.py`, add `import math` at the top, then add the function:

```python
DECAY_RATES = {
    "person": 0.001,
    "project": 0.005,
    "place": 0.003,
    "concept": 0.01,
    "default": 0.005,
}


def decay_salience(con: sqlite3.Connection) -> int:
    """Apply exponential decay to salience scores. Returns count updated."""
    entities = con.execute(
        "SELECT id, kind, salience_score, last_seen FROM entities WHERE salience_score > 0.05"
    ).fetchall()
    updated = 0
    now = datetime.now(timezone.utc)
    for eid, kind, salience, last_seen_str in entities:
        try:
            last = datetime.fromisoformat(last_seen_str.replace("Z", "+00:00"))
            days = (now - last).days
        except (ValueError, AttributeError):
            days = 30
        if days < 1:
            continue
        lam = DECAY_RATES.get(kind, DECAY_RATES["default"])
        new_salience = salience * math.exp(-lam * days)
        new_salience = max(0.05, new_salience)
        if abs(new_salience - salience) > 0.01:
            con.execute("UPDATE entities SET salience_score = ? WHERE id = ?", (round(new_salience, 4), eid))
            updated += 1
    return updated
```

Add to `run_consolidation`, after `questions_added`:

```python
    decay_count = decay_salience(con)
```

And add `"salience_decayed": decay_count` to the return dict.

- [ ] **Step 4: Run tests**

Run: `cd ~/dev/ai/pulse && python3 -m pytest scripts/tests/test_consolidate.py -v`

Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/pulse_consolidate.py scripts/tests/test_consolidate.py
git commit -m "feat: salience decay — exp(-λ*days) with kind-dependent rates"
```

---

### Task 7: Observability Metrics (Valence Trend + Extraction Efficiency)

**Files:**
- Modify: `scripts/pulse_consolidate.py`
- Modify: `scripts/tests/test_consolidate.py`

- [ ] **Step 1: Write the failing tests**

Add to `scripts/tests/test_consolidate.py`:

```python
def test_valence_trend_stable(tmp_path):
    """Valence trend with uniform sentiment should be 'stable'."""
    from pulse_consolidate import valence_trend
    con, _ = _fresh_db(tmp_path)
    # Insert 10 events with uniform sentiment
    for i in range(10):
        day = f"2026-04-{i+1:02d}T12:00:00Z"
        con.execute("INSERT INTO events (title, sentiment, ts) VALUES (?,?,?)", (f"e{i}", 0.5, day))

    result = valence_trend(con, days=30)
    assert result["trend"] in ("stable", "no_data", "insufficient")
    assert result["data_points"] >= 2


def test_extraction_efficiency(tmp_path):
    """Extraction efficiency should compute entities per 1K tokens."""
    from pulse_consolidate import extraction_efficiency
    con, _ = _fresh_db(tmp_path)
    now = "2026-04-16T00:00:00Z"
    # Add 5 entities
    for i in range(1, 6):
        con.execute("INSERT INTO entities (canonical_name, kind, first_seen, last_seen) VALUES (?,?,?,?)", (f"E{i}", "person", now, now))
    # Add 1 extraction job + metrics
    con.execute("INSERT INTO observations (source_kind, content_text, ts) VALUES ('test','test',?)", (now,))
    con.execute("INSERT INTO extraction_jobs (observation_id, state) VALUES (1,'done')")
    con.execute("INSERT INTO extraction_metrics (job_id, model, input_tokens, output_tokens) VALUES (1,'opus',5000,1000)")

    result = extraction_efficiency(con)
    assert result["total_entities"] == 5
    assert result["total_tokens"] == 6000
    assert result["entities_per_1k_tokens"] > 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/dev/ai/pulse && python3 -m pytest scripts/tests/test_consolidate.py::test_valence_trend_stable scripts/tests/test_consolidate.py::test_extraction_efficiency -v`

Expected: FAIL — functions don't exist.

- [ ] **Step 3: Implement observability metrics**

Add to `scripts/pulse_consolidate.py`:

```python
def valence_trend(con: sqlite3.Connection, days: int = 30) -> dict:
    """Compute sentiment trend from events over the last N days."""
    rows = con.execute(
        "SELECT DATE(ts) as day, AVG(sentiment) as avg_sent, COUNT(*) as n "
        "FROM events WHERE ts > DATE('now', ?) GROUP BY DATE(ts) ORDER BY day",
        (f"-{days} days",),
    ).fetchall()
    if not rows:
        return {"trend": "no_data", "days": days}
    sentiments = [r[1] for r in rows if r[1] is not None]
    if len(sentiments) < 2:
        return {"trend": "insufficient", "days": days, "data_points": len(sentiments)}
    recent_avg = sum(sentiments[-7:]) / len(sentiments[-7:]) if len(sentiments) >= 7 else sentiments[-1]
    overall_avg = sum(sentiments) / len(sentiments)
    return {
        "trend": "improving" if recent_avg > overall_avg + 0.1 else "declining" if recent_avg < overall_avg - 0.1 else "stable",
        "recent_7d_avg": round(recent_avg, 3),
        "overall_avg": round(overall_avg, 3),
        "days": days,
        "data_points": len(sentiments),
    }


def extraction_efficiency(con: sqlite3.Connection) -> dict:
    """Compute extraction efficiency: entities per 1K tokens."""
    row = con.execute(
        "SELECT COUNT(*) as jobs, SUM(input_tokens + output_tokens) as total_tokens "
        "FROM extraction_metrics"
    ).fetchone()
    entity_count = con.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    if not row or not row[1] or row[1] == 0:
        return {"entities_per_1k_tokens": 0, "total_jobs": 0}
    return {
        "entities_per_1k_tokens": round(entity_count / (row[1] / 1000), 2),
        "total_jobs": row[0],
        "total_tokens": row[1],
        "total_entities": entity_count,
    }
```

Add to `run_consolidation`, after `decay_count`:

```python
    trend = valence_trend(con)
    efficiency = extraction_efficiency(con)
```

And add to return dict:
```python
    "valence_trend": trend,
    "extraction_efficiency": efficiency,
```

- [ ] **Step 4: Run tests**

Run: `cd ~/dev/ai/pulse && python3 -m pytest scripts/tests/test_consolidate.py -v`

Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/pulse_consolidate.py scripts/tests/test_consolidate.py
git commit -m "feat: observability — valence trend + extraction efficiency metrics"
```

---

### Task 8: Full Suite + Integration Verification

**Files:**
- All modified files from Tasks 1-7

- [ ] **Step 1: Run full test suite**

Run: `cd ~/dev/ai/pulse && python3 -m pytest scripts/tests/ -v`

Expected: All tests PASS (87 existing + 10 new = 97+ total). Zero failures.

- [ ] **Step 2: Run consolidation end-to-end test on smoke DB**

Run: `cd ~/dev/ai/pulse && python3 -c "
from pulse_consolidate import run_consolidation
import json, sys
sys.path.insert(0, 'scripts')
report = run_consolidation('pulse-dev/pulse.db')
print(json.dumps(report, indent=2, default=str))
"`

Expected: Report includes all new fields: `implicit_relation_candidates`, `knowledge_gaps`, `questions_auto_added`, `salience_decayed`, `valence_trend`, `extraction_efficiency`. Skip-guard should NOT trigger on first run.

- [ ] **Step 3: Run retrieval depth test on smoke DB**

Run: `cd ~/dev/ai/pulse && python3 -c "
import sqlite3, json, sys
sys.path.insert(0, 'scripts')
from extract.retrieval import retrieve_context
con = sqlite3.connect('pulse-dev/pulse.db')
# Test depth=1 (default)
r1 = retrieve_context(con, 'Nik')
print(f'depth=1: {r1[\"total_matched\"]} entities, max_depth={r1.get(\"max_depth_used\", \"N/A\")}')
for e in r1['matched_entities'][:3]:
    print(f'  {e[\"canonical_name\"]} (hop={e.get(\"_hop\", \"?\")}, salience={e[\"salience_score\"]})')
# Test depth=2
r2 = retrieve_context(con, 'Nik', depth=2)
print(f'depth=2: {r2[\"total_matched\"]} entities, max_depth={r2.get(\"max_depth_used\", \"N/A\")}')
for e in r2['matched_entities'][:5]:
    print(f'  {e[\"canonical_name\"]} (hop={e.get(\"_hop\", \"?\")}, salience={e[\"salience_score\"]})')
con.close()
"`

Expected: depth=2 returns more entities than depth=1.

- [ ] **Step 4: Update consolidation CLI output**

Modify `main()` in `scripts/pulse_consolidate.py` to print new fields:

```python
def main() -> int:
    parser = argparse.ArgumentParser(description="Pulse graph consolidation")
    parser.add_argument("--db", required=True, help="Path to pulse database")
    args = parser.parse_args()

    report = run_consolidation(args.db)

    if report.get("skipped"):
        print(f"=== Consolidation SKIPPED: {report['reason']} ===")
        return 0

    print("=== Consolidation Report ===")
    print(f"Entities: {report['stats']['total_entities']} ({report['stats']['entities_by_kind']})")
    print(f"Orphans: {report['stats']['orphan_entities']}")
    print(f"Relations: {report['stats']['total_relations']}")
    print(f"Facts: {report['stats']['total_facts']}")
    print(f"Duplicate candidates: {report['duplicate_candidates']}")
    for dup in report["duplicates"]:
        print(f"  {dup['entity_a_name']} <-> {dup['entity_b_name']} ({dup['similarity']})")
    print(f"Implicit relation candidates: {len(report.get('implicit_relation_candidates', []))}")
    for irc in report.get("implicit_relation_candidates", [])[:5]:
        print(f"  {irc['entity_a_name']} <-> {irc['entity_b_name']} (co-occurs {irc['co_count']}x)")
    print(f"Knowledge gaps: {report.get('knowledge_gaps', 0)}")
    print(f"Questions auto-added: {report.get('questions_auto_added', 0)}")
    print(f"Salience decayed: {report.get('salience_decayed', 0)}")
    print(f"Stale questions closed: {report['stale_questions_closed']}")
    print(f"Merges executed: {report['merges_executed']}")
    vt = report.get("valence_trend", {})
    print(f"Valence trend: {vt.get('trend', 'N/A')} (recent={vt.get('recent_7d_avg', 'N/A')}, overall={vt.get('overall_avg', 'N/A')}, points={vt.get('data_points', 0)})")
    ee = report.get("extraction_efficiency", {})
    print(f"Extraction efficiency: {ee.get('entities_per_1k_tokens', 0)} entities/1K tokens ({ee.get('total_entities', 0)} entities, {ee.get('total_tokens', 0)} tokens)")
    return 0
```

- [ ] **Step 5: Run full suite one final time**

Run: `cd ~/dev/ai/pulse && python3 -m pytest scripts/tests/ -v`

Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/pulse_consolidate.py
git commit -m "feat: consolidation CLI — print all Phase 2e metrics"
```

---

## Self-Review Checklist

**Spec coverage:**
- [x] 2b.1 Adaptive depth retrieval → Task 2
- [x] 2b.2A Co-occurrence detection → Task 4
- [x] 2b.2B Knowledge gap detection → Task 5
- [x] 2b.2C Auto-populate open_questions → Task 5
- [x] 2b.2D Skip-guard → Task 3
- [x] 2b.3 Salience decay → Task 6
- [x] 2b.4A Valence trend → Task 7
- [x] 2b.4B Extraction efficiency → Task 7
- [x] Backwards-compatible (87 existing tests) → Task 8

**Placeholder scan:** No TBD/TODO/placeholders found.

**Type consistency:** All function signatures match between test and implementation code. `run_consolidation` return dict keys consistent across all tasks.
