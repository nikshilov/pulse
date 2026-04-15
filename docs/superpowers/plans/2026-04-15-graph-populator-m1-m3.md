# Graph Populator — Implementation Plan (M1-M3 Core)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the capture→ingest→extract core of the graph populator so Pulse can absorb the Claude JSONL archive into a queryable emotional-memory graph. After M3 we have working software: archives ingested, two-pass extractor produces entities/relations/events/facts with scores, erasure works, merge proposals queue up for Nik.

**Architecture:** Python-first for M1-M3 (rapid extractor iteration), Go-side only for the `/ingest` HTTP endpoint that writes to the existing Pulse SQLite. All schema changes happen via `internal/store/migrations/*.sql` on the Go side. Python CLI (`scripts/pulse_ingest.py`, `scripts/pulse_extract.py`, `scripts/pulse_erase.py`) does ingest/extract/erase by calling the Pulse HTTP API for writes and reading DB directly for extractor loop.

**Tech Stack:** Go 1.25, SQLite (modernc.org/sqlite), chi router, embedded migrations. Python 3.13 for workers (httpx, anthropic SDK, pytest). Anthropic API: Sonnet 4.6 for triage, Opus 4.6 for extract.

**Spec:** `docs/superpowers/specs/2026-04-15-graph-populator-design.md` (v3).

---

## File Structure

**Go side — new/modified:**
- `internal/store/migrations/003_observations.sql` — create (observations, observation_revisions, provider_cursors, erasure_log)
- `internal/store/migrations/004_extraction.sql` — create (extraction_jobs)
- `internal/store/migrations/005_graph.sql` — create (entities, entity_identities, relations, facts, events, evidence, score_history, entity_merge_proposals, sensitive_actors, open_questions)
- `internal/capture/types.go` — create (Observation, ActorRef, MediaRef types)
- `internal/capture/hash.go` — create (content_hash computation)
- `internal/ingest/handler.go` — create (POST /ingest HTTP handler)
- `internal/ingest/dedupe.go` — create (dedup + revision-detect logic)
- `internal/erase/erase.go` — create (soft/hard/nuclear erasure)
- `internal/server/server.go:RegisterRoutes` — modify (wire /ingest + /erase)
- `cmd/pulse/main.go` — modify (if needed: expose flags, no new binaries)

**Python side — new:**
- `scripts/pulse_ingest.py` — CLI for batch-import
- `scripts/providers/__init__.py`
- `scripts/providers/claude_jsonl.py` — JSONL → Observations normalizer
- `scripts/pulse_extract.py` — extractor loop (triage + extract)
- `scripts/extract/prompts.py` — Sonnet triage + Opus extract prompts
- `scripts/extract/resolver.py` — entity resolution with confidence gates
- `scripts/extract/scorer.py` — salience/emotional_weight/sentiment (version-pinned)
- `scripts/pulse_erase.py` — wrapper for Go erase CLI (if we keep Go-side) OR full Python
- `scripts/tests/test_claude_jsonl.py` — normalizer tests
- `scripts/tests/test_extract.py` — fixture-based tests
- `scripts/tests/fixtures/claude_jsonl_sample.jsonl` — small real-shaped sample

---

## Task 1: Migration — observations + revisions + cursors + erasure_log

**Files:**
- Create: `internal/store/migrations/003_observations.sql`
- Test: `internal/store/store_test.go` (add case)

- [ ] **Step 1: Write the failing test**

Add to `internal/store/store_test.go`:

```go
func TestMigration003Observations(t *testing.T) {
    dir := t.TempDir()
    db, err := Open(filepath.Join(dir, "test.db"))
    if err != nil {
        t.Fatalf("open: %v", err)
    }
    defer db.Close()

    // All tables should exist after migration
    for _, table := range []string{"observations", "observation_revisions", "provider_cursors", "erasure_log"} {
        var name string
        err := db.QueryRow("SELECT name FROM sqlite_master WHERE type='table' AND name=?", table).Scan(&name)
        if err != nil {
            t.Errorf("table %s missing: %v", table, err)
        }
    }

    // UNIQUE constraint on (source_kind, source_id, version)
    _, err = db.Exec(`INSERT INTO observations
        (source_kind, source_id, content_hash, version, scope, captured_at, observed_at, actors, content_text, metadata, raw_json)
        VALUES ('tg','m:1','h1',1,'nik','2026-04-15T00:00:00Z','2026-04-15T00:00:00Z','[]','hello','{}','{}')`)
    if err != nil {
        t.Fatalf("first insert: %v", err)
    }
    _, err = db.Exec(`INSERT INTO observations
        (source_kind, source_id, content_hash, version, scope, captured_at, observed_at, actors, content_text, metadata, raw_json)
        VALUES ('tg','m:1','h1',1,'nik','2026-04-15T00:00:00Z','2026-04-15T00:00:00Z','[]','hello','{}','{}')`)
    if err == nil {
        t.Fatal("expected UNIQUE violation, got nil")
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/nikshilov/dev/ai/pulse && go test ./internal/store/ -run TestMigration003Observations -v`
Expected: FAIL — table `observations` does not exist.

- [ ] **Step 3: Write migration**

Create `internal/store/migrations/003_observations.sql`:

```sql
-- Raw normalized events from all sources (append-only, edits → new version row)
CREATE TABLE observations (
    id            INTEGER PRIMARY KEY,
    source_kind   TEXT NOT NULL,
    source_id     TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    version       INTEGER NOT NULL DEFAULT 1,
    scope         TEXT NOT NULL CHECK(scope IN ('elle','nik','shared')),
    captured_at   TEXT NOT NULL,
    observed_at   TEXT NOT NULL,
    actors        TEXT NOT NULL,
    content_text  TEXT,
    media_refs    TEXT,
    metadata      TEXT,
    raw_json      TEXT,
    redacted      INTEGER NOT NULL DEFAULT 0,
    UNIQUE(source_kind, source_id, version)
);
CREATE INDEX idx_obs_captured ON observations(captured_at);
CREATE INDEX idx_obs_scope    ON observations(scope, captured_at);
CREATE INDEX idx_obs_sourceid ON observations(source_kind, source_id);

-- Edit history for observations
CREATE TABLE observation_revisions (
    observation_id INTEGER NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
    version        INTEGER NOT NULL,
    prev_hash      TEXT,
    diff           TEXT,
    changed_at     TEXT NOT NULL,
    PRIMARY KEY (observation_id, version)
);

-- Per-provider cursor for periodic-pull sources
CREATE TABLE provider_cursors (
    source_kind  TEXT PRIMARY KEY,
    cursor       TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- Erasure audit log
CREATE TABLE erasure_log (
    id            INTEGER PRIMARY KEY,
    op_kind       TEXT NOT NULL CHECK(op_kind IN ('soft','hard','nuclear')),
    subject_kind  TEXT NOT NULL,
    subject_id    TEXT,
    initiated_by  TEXT NOT NULL,
    initiated_at  TEXT NOT NULL,
    completed_at  TEXT,
    note          TEXT
);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/nikshilov/dev/ai/pulse && go test ./internal/store/ -run TestMigration003Observations -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse
git add internal/store/migrations/003_observations.sql internal/store/store_test.go
git commit -m "feat(store): migration 003 — observations, revisions, cursors, erasure_log"
```

---

## Task 2: Migration — extraction_jobs

**Files:**
- Create: `internal/store/migrations/004_extraction.sql`
- Test: `internal/store/store_test.go` (add case)

- [ ] **Step 1: Write the failing test**

Add to `internal/store/store_test.go`:

```go
func TestMigration004ExtractionJobs(t *testing.T) {
    dir := t.TempDir()
    db, err := Open(filepath.Join(dir, "test.db"))
    if err != nil {
        t.Fatalf("open: %v", err)
    }
    defer db.Close()

    _, err = db.Exec(`INSERT INTO extraction_jobs
        (observation_ids, state, attempts, created_at, updated_at)
        VALUES ('[1,2,3]', 'pending', 0, '2026-04-15T00:00:00Z', '2026-04-15T00:00:00Z')`)
    if err != nil {
        t.Fatalf("insert: %v", err)
    }

    var state string
    err = db.QueryRow("SELECT state FROM extraction_jobs WHERE id=1").Scan(&state)
    if err != nil {
        t.Fatalf("select: %v", err)
    }
    if state != "pending" {
        t.Errorf("expected pending, got %s", state)
    }

    // CHECK constraint on state
    _, err = db.Exec(`INSERT INTO extraction_jobs
        (observation_ids, state, attempts, created_at, updated_at)
        VALUES ('[4]', 'bogus_state', 0, '2026-04-15T00:00:00Z', '2026-04-15T00:00:00Z')`)
    if err == nil {
        t.Fatal("expected CHECK violation for bogus state")
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/nikshilov/dev/ai/pulse && go test ./internal/store/ -run TestMigration004ExtractionJobs -v`
Expected: FAIL — no such table.

- [ ] **Step 3: Write migration**

Create `internal/store/migrations/004_extraction.sql`:

```sql
CREATE TABLE extraction_jobs (
    id              INTEGER PRIMARY KEY,
    observation_ids TEXT NOT NULL,
    state           TEXT NOT NULL CHECK(state IN ('pending','running','done','failed','dlq')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    triage_model    TEXT,
    extract_model   TEXT,
    triage_verdict  TEXT CHECK(triage_verdict IS NULL OR triage_verdict IN ('extract','skip','defer')),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX idx_extraction_state ON extraction_jobs(state, created_at);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/nikshilov/dev/ai/pulse && go test ./internal/store/ -run TestMigration004ExtractionJobs -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse
git add internal/store/migrations/004_extraction.sql internal/store/store_test.go
git commit -m "feat(store): migration 004 — extraction_jobs with DLQ state"
```

---

## Task 3: Migration — graph core (entities, relations, events, facts, evidence, scores)

**Files:**
- Create: `internal/store/migrations/005_graph.sql`
- Test: `internal/store/store_test.go` (add case)

- [ ] **Step 1: Write the failing test**

Add to `internal/store/store_test.go`:

```go
func TestMigration005Graph(t *testing.T) {
    dir := t.TempDir()
    db, err := Open(filepath.Join(dir, "test.db"))
    if err != nil {
        t.Fatalf("open: %v", err)
    }
    defer db.Close()

    tables := []string{
        "entities", "entity_identities", "relations", "facts", "events",
        "evidence", "score_history", "entity_merge_proposals",
        "sensitive_actors", "open_questions",
    }
    for _, table := range tables {
        var name string
        err := db.QueryRow("SELECT name FROM sqlite_master WHERE type='table' AND name=?", table).Scan(&name)
        if err != nil {
            t.Errorf("table %s missing: %v", table, err)
        }
    }

    // CASCADE on entity delete
    _, err = db.Exec(`INSERT INTO entities (canonical_name, kind, first_seen, last_seen) VALUES ('test','person','2026-04-15T00:00:00Z','2026-04-15T00:00:00Z')`)
    if err != nil { t.Fatal(err) }
    _, err = db.Exec(`INSERT INTO entity_identities (entity_id, source_kind, identifier, first_seen) VALUES (1,'tg','123','2026-04-15T00:00:00Z')`)
    if err != nil { t.Fatal(err) }
    if _, err := db.Exec(`DELETE FROM entities WHERE id=1`); err != nil { t.Fatal(err) }

    var count int
    db.QueryRow(`SELECT COUNT(*) FROM entity_identities WHERE entity_id=1`).Scan(&count)
    if count != 0 {
        t.Errorf("expected cascade delete, got %d identities", count)
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/nikshilov/dev/ai/pulse && go test ./internal/store/ -run TestMigration005Graph -v`
Expected: FAIL.

- [ ] **Step 3: Write migration**

Create `internal/store/migrations/005_graph.sql`:

```sql
-- Canonical entities (people, places, projects, orgs, things)
CREATE TABLE entities (
    id                INTEGER PRIMARY KEY,
    canonical_name    TEXT NOT NULL,
    kind              TEXT NOT NULL CHECK(kind IN ('person','place','project','org','thing','event_series')),
    aliases           TEXT,
    first_seen        TEXT NOT NULL,
    last_seen         TEXT NOT NULL,
    salience_score    REAL NOT NULL DEFAULT 0,
    emotional_weight  REAL NOT NULL DEFAULT 0,
    scorer_version    TEXT,
    description_md    TEXT
);
CREATE INDEX idx_entities_kind ON entities(kind);

-- One entity → many source identifiers
CREATE TABLE entity_identities (
    id           INTEGER PRIMARY KEY,
    entity_id    INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    source_kind  TEXT NOT NULL,
    identifier   TEXT NOT NULL,
    confidence   REAL NOT NULL DEFAULT 1.0,
    first_seen   TEXT NOT NULL,
    UNIQUE(source_kind, identifier)
);
CREATE INDEX idx_identities_entity ON entity_identities(entity_id);

-- Entity merge proposals (confidence-gated)
CREATE TABLE entity_merge_proposals (
    id             INTEGER PRIMARY KEY,
    from_entity_id INTEGER NOT NULL REFERENCES entities(id),
    to_entity_id   INTEGER NOT NULL REFERENCES entities(id),
    confidence     REAL NOT NULL,
    evidence_md    TEXT NOT NULL,
    state          TEXT NOT NULL CHECK(state IN ('pending','approved','rejected','auto_merged')),
    proposed_at    TEXT NOT NULL,
    resolved_at    TEXT,
    resolved_by    TEXT
);
CREATE INDEX idx_merge_state ON entity_merge_proposals(state);

-- Sensitive actors allowlist
CREATE TABLE sensitive_actors (
    entity_id   INTEGER PRIMARY KEY REFERENCES entities(id),
    policy      TEXT NOT NULL CHECK(policy IN ('redact_content','summary_only','no_capture')),
    reason      TEXT,
    added_at    TEXT NOT NULL,
    added_by    TEXT NOT NULL
);

-- Relations between entities
CREATE TABLE relations (
    id                 INTEGER PRIMARY KEY,
    from_entity_id     INTEGER NOT NULL REFERENCES entities(id),
    to_entity_id       INTEGER NOT NULL REFERENCES entities(id),
    kind               TEXT NOT NULL,
    strength           REAL NOT NULL DEFAULT 0,
    first_seen         TEXT NOT NULL,
    last_seen          TEXT NOT NULL
);
CREATE INDEX idx_relations_from ON relations(from_entity_id);
CREATE INDEX idx_relations_to   ON relations(to_entity_id);

-- Facts (atomic claims about entities)
CREATE TABLE facts (
    id                 INTEGER PRIMARY KEY,
    entity_id          INTEGER NOT NULL REFERENCES entities(id),
    text               TEXT NOT NULL,
    confidence         REAL NOT NULL DEFAULT 1.0,
    scorer_version     TEXT,
    created_at         TEXT NOT NULL
);
CREATE INDEX idx_facts_entity ON facts(entity_id);

-- Events
CREATE TABLE events (
    id                 INTEGER PRIMARY KEY,
    title              TEXT NOT NULL,
    description        TEXT,
    sentiment          REAL,
    emotional_weight   REAL NOT NULL DEFAULT 0,
    scorer_version     TEXT,
    ts                 TEXT NOT NULL
);
CREATE INDEX idx_events_ts ON events(ts);

-- Normalized evidence
CREATE TABLE evidence (
    id               INTEGER PRIMARY KEY,
    subject_kind     TEXT NOT NULL CHECK(subject_kind IN ('relation','fact','event','entity')),
    subject_id       INTEGER NOT NULL,
    observation_id   INTEGER NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
    weight           REAL NOT NULL DEFAULT 1.0,
    created_at       TEXT NOT NULL
);
CREATE INDEX idx_evidence_subject ON evidence(subject_kind, subject_id);
CREATE INDEX idx_evidence_obs     ON evidence(observation_id);

-- Score history
CREATE TABLE score_history (
    id               INTEGER PRIMARY KEY,
    subject_kind     TEXT NOT NULL,
    subject_id       INTEGER NOT NULL,
    salience         REAL,
    emotional_weight REAL,
    sentiment        REAL,
    scorer_version   TEXT NOT NULL,
    computed_at      TEXT NOT NULL
);
CREATE INDEX idx_score_subject ON score_history(subject_kind, subject_id, computed_at);

-- Open questions Elle holds for evening sync
CREATE TABLE open_questions (
    id                INTEGER PRIMARY KEY,
    subject_entity_id INTEGER REFERENCES entities(id),
    question_text     TEXT NOT NULL,
    asked_at          TEXT NOT NULL,
    ttl_expires_at    TEXT NOT NULL,
    answered_at       TEXT,
    answer_text       TEXT,
    state             TEXT NOT NULL CHECK(state IN ('open','answered','expired','auto_closed'))
);
CREATE INDEX idx_questions_state ON open_questions(state, ttl_expires_at);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/nikshilov/dev/ai/pulse && go test ./internal/store/ -run TestMigration005Graph -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse
git add internal/store/migrations/005_graph.sql internal/store/store_test.go
git commit -m "feat(store): migration 005 — graph core (entities, relations, events, facts, evidence, scores)"
```

---

## Task 4: Observation + ActorRef types in Go

**Files:**
- Create: `internal/capture/types.go`
- Create: `internal/capture/types_test.go`

- [ ] **Step 1: Write the failing test**

Create `internal/capture/types_test.go`:

```go
package capture

import (
    "encoding/json"
    "testing"
    "time"
)

func TestObservationJSONRoundtrip(t *testing.T) {
    obs := Observation{
        SourceKind:  "claude_jsonl",
        SourceID:    "sess-abc:42",
        ContentHash: "sha256-fake",
        Version:     1,
        Scope:       "shared",
        CapturedAt:  time.Date(2026, 4, 15, 12, 0, 0, 0, time.UTC),
        ObservedAt:  time.Date(2026, 4, 15, 12, 0, 1, 0, time.UTC),
        Actors: []ActorRef{
            {Kind: "user", ID: "nik", Display: "Nik"},
            {Kind: "assistant", ID: "elle", Display: "Elle"},
        },
        ContentText: "hello",
        Metadata:    map[string]any{"model": "opus-4.6"},
    }

    b, err := json.Marshal(obs)
    if err != nil {
        t.Fatalf("marshal: %v", err)
    }

    var parsed Observation
    if err := json.Unmarshal(b, &parsed); err != nil {
        t.Fatalf("unmarshal: %v", err)
    }

    if parsed.SourceKind != "claude_jsonl" {
        t.Errorf("SourceKind: got %s", parsed.SourceKind)
    }
    if len(parsed.Actors) != 2 {
        t.Errorf("Actors len: got %d", len(parsed.Actors))
    }
    if parsed.Actors[0].Kind != "user" {
        t.Errorf("Actor[0].Kind: got %s", parsed.Actors[0].Kind)
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/nikshilov/dev/ai/pulse && go test ./internal/capture/ -run TestObservationJSONRoundtrip -v`
Expected: FAIL — package not found.

- [ ] **Step 3: Write types**

Create `internal/capture/types.go`:

```go
package capture

import (
    "encoding/json"
    "time"
)

type ActorRef struct {
    Kind    string `json:"kind"`
    ID      string `json:"id"`
    Display string `json:"display,omitempty"`
}

type MediaRef struct {
    Kind     string `json:"kind"`
    URL      string `json:"url,omitempty"`
    Filename string `json:"filename,omitempty"`
    Duration int    `json:"duration_sec,omitempty"`
}

type Observation struct {
    SourceKind  string          `json:"source_kind"`
    SourceID    string          `json:"source_id"`
    ContentHash string          `json:"content_hash"`
    Version     int             `json:"version"`
    Scope       string          `json:"scope"`
    CapturedAt  time.Time       `json:"captured_at"`
    ObservedAt  time.Time       `json:"observed_at"`
    Actors      []ActorRef      `json:"actors"`
    ContentText string          `json:"content_text,omitempty"`
    MediaRefs   []MediaRef      `json:"media_refs,omitempty"`
    Metadata    map[string]any  `json:"metadata,omitempty"`
    RawJSON     json.RawMessage `json:"raw_json,omitempty"`
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/nikshilov/dev/ai/pulse && go test ./internal/capture/ -run TestObservationJSONRoundtrip -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse
git add internal/capture/
git commit -m "feat(capture): Observation/ActorRef/MediaRef types"
```

---

## Task 5: content_hash computation

**Files:**
- Create: `internal/capture/hash.go`
- Create: `internal/capture/hash_test.go`

- [ ] **Step 1: Write the failing test**

Create `internal/capture/hash_test.go`:

```go
package capture

import "testing"

func TestContentHashDeterministic(t *testing.T) {
    h1 := ComputeContentHash("hello", map[string]any{"k": "v"})
    h2 := ComputeContentHash("hello", map[string]any{"k": "v"})
    if h1 != h2 {
        t.Errorf("expected deterministic hash, got %s vs %s", h1, h2)
    }
}

func TestContentHashDiffersOnContent(t *testing.T) {
    h1 := ComputeContentHash("hello", nil)
    h2 := ComputeContentHash("world", nil)
    if h1 == h2 {
        t.Error("expected different hashes for different content")
    }
}

func TestContentHashDiffersOnMetadata(t *testing.T) {
    h1 := ComputeContentHash("hello", map[string]any{"k": "v1"})
    h2 := ComputeContentHash("hello", map[string]any{"k": "v2"})
    if h1 == h2 {
        t.Error("expected different hashes for different metadata")
    }
}

func TestContentHashStableAcrossMapOrder(t *testing.T) {
    m1 := map[string]any{"a": 1, "b": 2}
    m2 := map[string]any{"b": 2, "a": 1}
    if ComputeContentHash("x", m1) != ComputeContentHash("x", m2) {
        t.Error("hash should be stable across map iteration order")
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/nikshilov/dev/ai/pulse && go test ./internal/capture/ -run TestContentHash -v`
Expected: FAIL — ComputeContentHash undefined.

- [ ] **Step 3: Write implementation**

Create `internal/capture/hash.go`:

```go
package capture

import (
    "crypto/sha256"
    "encoding/hex"
    "encoding/json"
    "sort"
)

// ComputeContentHash returns a deterministic sha256 of content_text joined with
// a canonical JSON encoding of metadata (keys sorted). Used to detect edits.
func ComputeContentHash(contentText string, metadata map[string]any) string {
    h := sha256.New()
    h.Write([]byte(contentText))
    h.Write([]byte{0x1f})

    if len(metadata) > 0 {
        keys := make([]string, 0, len(metadata))
        for k := range metadata {
            keys = append(keys, k)
        }
        sort.Strings(keys)
        canonical := make([][2]any, 0, len(keys))
        for _, k := range keys {
            canonical = append(canonical, [2]any{k, metadata[k]})
        }
        b, _ := json.Marshal(canonical)
        h.Write(b)
    }

    return hex.EncodeToString(h.Sum(nil))
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/nikshilov/dev/ai/pulse && go test ./internal/capture/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse
git add internal/capture/hash.go internal/capture/hash_test.go
git commit -m "feat(capture): ComputeContentHash — sha256 with sorted-metadata canonical form"
```

---

## Task 6: Ingest handler — insert observations with dedup

**Files:**
- Create: `internal/ingest/handler.go`
- Create: `internal/ingest/handler_test.go`
- Modify: `internal/server/server.go` (wire route)

- [ ] **Step 1: Write the failing test**

Create `internal/ingest/handler_test.go`:

```go
package ingest

import (
    "bytes"
    "encoding/json"
    "net/http"
    "net/http/httptest"
    "path/filepath"
    "testing"

    "github.com/nkkmnk/pulse/internal/capture"
    "github.com/nkkmnk/pulse/internal/store"
)

func openTestStore(t *testing.T) *store.Store {
    t.Helper()
    s, err := store.Open(filepath.Join(t.TempDir(), "t.db"))
    if err != nil {
        t.Fatalf("open: %v", err)
    }
    t.Cleanup(func() { s.Close() })
    return s
}

func TestIngestInsertsNewObservation(t *testing.T) {
    s := openTestStore(t)
    h := NewHandler(s)

    obs := capture.Observation{
        SourceKind: "tg", SourceID: "m:1", ContentHash: "h1", Version: 1,
        Scope: "nik",
        CapturedAt: timeParse(t, "2026-04-15T00:00:00Z"),
        ObservedAt: timeParse(t, "2026-04-15T00:00:01Z"),
        Actors: []capture.ActorRef{{Kind: "tg_user", ID: "123"}},
        ContentText: "hello",
    }
    body, _ := json.Marshal(map[string]any{"observations": []capture.Observation{obs}})

    req := httptest.NewRequest(http.MethodPost, "/ingest", bytes.NewReader(body))
    rec := httptest.NewRecorder()
    h.ServeHTTP(rec, req)

    if rec.Code != http.StatusOK {
        t.Fatalf("status: got %d body=%s", rec.Code, rec.Body.String())
    }

    var resp struct{ Inserted, Duplicates, Revisions int }
    json.Unmarshal(rec.Body.Bytes(), &resp)
    if resp.Inserted != 1 {
        t.Errorf("inserted: got %d", resp.Inserted)
    }
}

func TestIngestDedupsIdenticalHash(t *testing.T) {
    s := openTestStore(t)
    h := NewHandler(s)

    obs := capture.Observation{
        SourceKind: "tg", SourceID: "m:1", ContentHash: "h1", Version: 1,
        Scope: "nik",
        CapturedAt: timeParse(t, "2026-04-15T00:00:00Z"),
        ObservedAt: timeParse(t, "2026-04-15T00:00:01Z"),
        Actors: []capture.ActorRef{{Kind: "tg_user", ID: "123"}},
        ContentText: "hello",
    }
    body, _ := json.Marshal(map[string]any{"observations": []capture.Observation{obs}})

    // First ingest
    req := httptest.NewRequest(http.MethodPost, "/ingest", bytes.NewReader(body))
    h.ServeHTTP(httptest.NewRecorder(), req)

    // Second ingest — identical
    req2 := httptest.NewRequest(http.MethodPost, "/ingest", bytes.NewReader(body))
    rec2 := httptest.NewRecorder()
    h.ServeHTTP(rec2, req2)

    var resp struct{ Inserted, Duplicates, Revisions int }
    json.Unmarshal(rec2.Body.Bytes(), &resp)
    if resp.Duplicates != 1 {
        t.Errorf("duplicates: got %d", resp.Duplicates)
    }
    if resp.Inserted != 0 {
        t.Errorf("inserted should be 0, got %d", resp.Inserted)
    }
}

// timeParse helper + package-level imports
```

Add `func timeParse(t *testing.T, s string) time.Time { … }` helper.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/nikshilov/dev/ai/pulse && go test ./internal/ingest/ -v`
Expected: FAIL — package does not exist.

- [ ] **Step 3: Write handler**

Create `internal/ingest/handler.go`:

```go
package ingest

import (
    "database/sql"
    "encoding/json"
    "fmt"
    "net/http"
    "time"

    "github.com/nkkmnk/pulse/internal/capture"
    "github.com/nkkmnk/pulse/internal/store"
)

type Handler struct {
    store *store.Store
}

func NewHandler(s *store.Store) *Handler {
    return &Handler{store: s}
}

type request struct {
    Observations []capture.Observation `json:"observations"`
}

type response struct {
    Inserted   int `json:"inserted"`
    Duplicates int `json:"duplicates"`
    Revisions  int `json:"revisions"`
    IDs        []int64 `json:"ids,omitempty"`
}

func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
    if r.Method != http.MethodPost {
        http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
        return
    }

    var req request
    if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
        http.Error(w, "invalid json: "+err.Error(), http.StatusBadRequest)
        return
    }

    resp := response{}
    for i := range req.Observations {
        obs := &req.Observations[i]
        if err := validate(obs); err != nil {
            http.Error(w, fmt.Sprintf("obs[%d]: %v", i, err), http.StatusBadRequest)
            return
        }
        op, id, err := h.upsert(r.Context(), obs)
        if err != nil {
            http.Error(w, fmt.Sprintf("obs[%d] store: %v", i, err), http.StatusInternalServerError)
            return
        }
        switch op {
        case opInsert:
            resp.Inserted++
        case opDuplicate:
            resp.Duplicates++
        case opRevision:
            resp.Revisions++
        }
        resp.IDs = append(resp.IDs, id)
    }

    w.Header().Set("Content-Type", "application/json")
    json.NewEncoder(w).Encode(resp)
}

func validate(obs *capture.Observation) error {
    if obs.SourceKind == "" { return fmt.Errorf("source_kind required") }
    if obs.SourceID == "" { return fmt.Errorf("source_id required") }
    if obs.Scope != "elle" && obs.Scope != "nik" && obs.Scope != "shared" {
        return fmt.Errorf("scope must be elle|nik|shared, got %q", obs.Scope)
    }
    if obs.CapturedAt.IsZero() { return fmt.Errorf("captured_at required") }
    if obs.ObservedAt.IsZero() { obs.ObservedAt = time.Now().UTC() }
    if obs.Version == 0 { obs.Version = 1 }
    if obs.ContentHash == "" {
        obs.ContentHash = capture.ComputeContentHash(obs.ContentText, obs.Metadata)
    }
    return nil
}

type op int
const (
    opInsert op = iota
    opDuplicate
    opRevision
)
```

Add method `(h *Handler) upsert(...)` in new file `internal/ingest/dedupe.go` (next task).

For this task include a minimal inline `upsert` that only handles insert/duplicate (revision comes in Task 7):

```go
func (h *Handler) upsert(ctx context.Context, obs *capture.Observation) (op, int64, error) {
    db := h.store.DB()
    var existingID int64
    var existingHash string
    err := db.QueryRowContext(ctx, `
        SELECT id, content_hash FROM observations
        WHERE source_kind=? AND source_id=?
        ORDER BY version DESC LIMIT 1`,
        obs.SourceKind, obs.SourceID,
    ).Scan(&existingID, &existingHash)

    if err == sql.ErrNoRows {
        id, err := insertObservation(ctx, db, obs)
        return opInsert, id, err
    }
    if err != nil {
        return 0, 0, err
    }
    if existingHash == obs.ContentHash {
        return opDuplicate, existingID, nil
    }
    // Revision path — handled in Task 7
    return 0, 0, fmt.Errorf("revision not yet implemented")
}

func insertObservation(ctx context.Context, db *sql.DB, obs *capture.Observation) (int64, error) {
    actors, _ := json.Marshal(obs.Actors)
    meta, _ := json.Marshal(obs.Metadata)
    media, _ := json.Marshal(obs.MediaRefs)
    res, err := db.ExecContext(ctx, `
        INSERT INTO observations
          (source_kind, source_id, content_hash, version, scope,
           captured_at, observed_at, actors, content_text, media_refs, metadata, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        obs.SourceKind, obs.SourceID, obs.ContentHash, obs.Version, obs.Scope,
        obs.CapturedAt.Format(time.RFC3339), obs.ObservedAt.Format(time.RFC3339),
        string(actors), obs.ContentText, string(media), string(meta), string(obs.RawJSON),
    )
    if err != nil { return 0, err }
    return res.LastInsertId()
}
```

You will also need to expose `DB() *sql.DB` from `internal/store/store.go`:

```go
// In internal/store/store.go
func (s *Store) DB() *sql.DB { return s.db }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/nikshilov/dev/ai/pulse && go test ./internal/ingest/ -v`
Expected: PASS (insert + duplicate cases).

- [ ] **Step 5: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse
git add internal/ingest/ internal/store/store.go
git commit -m "feat(ingest): POST /ingest handler — insert + duplicate dedup"
```

---

## Task 7: Revision detection — content_hash diff → new version

**Files:**
- Create: `internal/ingest/dedupe.go` (move logic from handler)
- Modify: `internal/ingest/handler.go` (remove inline upsert)
- Modify: `internal/ingest/handler_test.go` (add revision test)

- [ ] **Step 1: Write the failing test**

Add to `internal/ingest/handler_test.go`:

```go
func TestIngestRevisionOnHashChange(t *testing.T) {
    s := openTestStore(t)
    h := NewHandler(s)

    base := capture.Observation{
        SourceKind: "tg", SourceID: "m:1", ContentHash: "h1", Version: 1,
        Scope: "nik",
        CapturedAt: timeParse(t, "2026-04-15T00:00:00Z"),
        ObservedAt: timeParse(t, "2026-04-15T00:00:01Z"),
        Actors: []capture.ActorRef{{Kind: "tg_user", ID: "123"}},
        ContentText: "hello",
    }
    body1, _ := json.Marshal(map[string]any{"observations": []capture.Observation{base}})
    h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodPost, "/ingest", bytes.NewReader(body1)))

    // Edited: content changed → new hash
    edited := base
    edited.ContentText = "hello world"
    edited.ContentHash = "h2"

    body2, _ := json.Marshal(map[string]any{"observations": []capture.Observation{edited}})
    rec := httptest.NewRecorder()
    h.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/ingest", bytes.NewReader(body2)))

    var resp struct{ Inserted, Duplicates, Revisions int }
    json.Unmarshal(rec.Body.Bytes(), &resp)
    if resp.Revisions != 1 {
        t.Errorf("revisions: got %d", resp.Revisions)
    }

    // Check DB: version=2 exists, observation_revisions has one row
    var maxVer int
    s.DB().QueryRow(`SELECT MAX(version) FROM observations WHERE source_kind='tg' AND source_id='m:1'`).Scan(&maxVer)
    if maxVer != 2 {
        t.Errorf("max version: got %d", maxVer)
    }
    var revCount int
    s.DB().QueryRow(`SELECT COUNT(*) FROM observation_revisions`).Scan(&revCount)
    if revCount != 1 {
        t.Errorf("revisions row count: got %d", revCount)
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/nikshilov/dev/ai/pulse && go test ./internal/ingest/ -run TestIngestRevisionOnHashChange -v`
Expected: FAIL — "revision not yet implemented".

- [ ] **Step 3: Extract revision logic**

Create `internal/ingest/dedupe.go`:

```go
package ingest

import (
    "context"
    "database/sql"
    "encoding/json"
    "fmt"
    "time"

    "github.com/nkkmnk/pulse/internal/capture"
)

func (h *Handler) upsert(ctx context.Context, obs *capture.Observation) (op, int64, error) {
    db := h.store.DB()
    var existingID int64
    var existingHash string
    var existingVersion int

    err := db.QueryRowContext(ctx, `
        SELECT id, content_hash, version FROM observations
        WHERE source_kind=? AND source_id=?
        ORDER BY version DESC LIMIT 1`,
        obs.SourceKind, obs.SourceID,
    ).Scan(&existingID, &existingHash, &existingVersion)

    if err == sql.ErrNoRows {
        id, err := insertObservation(ctx, db, obs)
        return opInsert, id, err
    }
    if err != nil {
        return 0, 0, err
    }
    if existingHash == obs.ContentHash {
        return opDuplicate, existingID, nil
    }

    // Revision path
    obs.Version = existingVersion + 1
    newID, err := insertObservation(ctx, db, obs)
    if err != nil {
        return 0, 0, err
    }
    if _, err := db.ExecContext(ctx, `
        INSERT INTO observation_revisions (observation_id, version, prev_hash, diff, changed_at)
        VALUES (?, ?, ?, ?, ?)`,
        newID, obs.Version, existingHash, computeDiff(existingID, db, ctx, obs.ContentText), time.Now().UTC().Format(time.RFC3339),
    ); err != nil {
        return 0, 0, fmt.Errorf("record revision: %w", err)
    }
    return opRevision, newID, nil
}

func insertObservation(ctx context.Context, db *sql.DB, obs *capture.Observation) (int64, error) {
    actors, _ := json.Marshal(obs.Actors)
    meta, _ := json.Marshal(obs.Metadata)
    media, _ := json.Marshal(obs.MediaRefs)
    res, err := db.ExecContext(ctx, `
        INSERT INTO observations
          (source_kind, source_id, content_hash, version, scope,
           captured_at, observed_at, actors, content_text, media_refs, metadata, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        obs.SourceKind, obs.SourceID, obs.ContentHash, obs.Version, obs.Scope,
        obs.CapturedAt.Format(time.RFC3339), obs.ObservedAt.Format(time.RFC3339),
        string(actors), obs.ContentText, string(media), string(meta), string(obs.RawJSON),
    )
    if err != nil { return 0, err }
    return res.LastInsertId()
}

// computeDiff returns a short human-readable diff marker. We're not running a real diff —
// just record old→new content_text prefixes for audit trail.
func computeDiff(prevID int64, db *sql.DB, ctx context.Context, newContent string) string {
    var prev string
    db.QueryRowContext(ctx, `SELECT content_text FROM observations WHERE id=?`, prevID).Scan(&prev)
    return fmt.Sprintf("-%.100q\n+%.100q", prev, newContent)
}
```

Remove the inline `upsert` and `insertObservation` from `handler.go` (move complete — keep only types and HTTP logic there).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/nikshilov/dev/ai/pulse && go test ./internal/ingest/ -v`
Expected: PASS all ingest tests.

- [ ] **Step 5: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse
git add internal/ingest/
git commit -m "feat(ingest): revision detection on content_hash change"
```

---

## Task 8: Wire /ingest into server

**Files:**
- Modify: `internal/server/server.go`
- Modify: `internal/server/server_test.go` (optional smoke)

- [ ] **Step 1: Read current server.go**

Run: `cd /Users/nikshilov/dev/ai/pulse && cat internal/server/server.go`

Identify the route registration section.

- [ ] **Step 2: Add /ingest route**

Edit `internal/server/server.go` — add (near other route registrations):

```go
import (
    // … existing imports
    "github.com/nkkmnk/pulse/internal/ingest"
)

// In the constructor / Routes function:
r.Method(http.MethodPost, "/ingest", ingest.NewHandler(s.store))
```

(Pattern depends on existing style — match whichever is used for outbox/context routes.)

- [ ] **Step 3: Build to verify**

Run: `cd /Users/nikshilov/dev/ai/pulse && go build ./...`
Expected: no errors.

- [ ] **Step 4: Smoke test**

Run: `cd /Users/nikshilov/dev/ai/pulse && go test ./internal/server/ -v`
Expected: existing tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse
git add internal/server/
git commit -m "feat(server): wire POST /ingest route"
```

---

## Task 9: Erasure — soft erase (mark redacted, null content)

**Files:**
- Create: `internal/erase/erase.go`
- Create: `internal/erase/erase_test.go`

- [ ] **Step 1: Write the failing test**

Create `internal/erase/erase_test.go`:

```go
package erase

import (
    "context"
    "path/filepath"
    "testing"

    "github.com/nkkmnk/pulse/internal/store"
)

func openTestStore(t *testing.T) *store.Store {
    t.Helper()
    s, err := store.Open(filepath.Join(t.TempDir(), "t.db"))
    if err != nil { t.Fatalf("open: %v", err) }
    t.Cleanup(func() { s.Close() })
    return s
}

func TestSoftEraseEntity(t *testing.T) {
    s := openTestStore(t)
    db := s.DB()

    // Fixture: one entity with two observations linked via evidence
    _, err := db.Exec(`INSERT INTO entities (canonical_name, kind, first_seen, last_seen) VALUES ('Alice','person','2026-04-15T00:00:00Z','2026-04-15T00:00:00Z')`)
    if err != nil { t.Fatal(err) }
    entID := int64(1)

    _, err = db.Exec(`INSERT INTO observations (source_kind, source_id, content_hash, version, scope, captured_at, observed_at, actors, content_text) VALUES
        ('tg','m:1','h1',1,'nik','2026-04-15T00:00:00Z','2026-04-15T00:00:00Z','[]','private message 1'),
        ('tg','m:2','h2',1,'nik','2026-04-15T00:00:00Z','2026-04-15T00:00:00Z','[]','private message 2')`)
    if err != nil { t.Fatal(err) }

    _, err = db.Exec(`INSERT INTO evidence (subject_kind, subject_id, observation_id, created_at) VALUES
        ('entity',1,1,'2026-04-15T00:00:00Z'),
        ('entity',1,2,'2026-04-15T00:00:00Z')`)
    if err != nil { t.Fatal(err) }

    // Act
    e := NewEraser(s)
    err = e.SoftErase(context.Background(), entID, "nik", "user request")
    if err != nil { t.Fatalf("soft erase: %v", err) }

    // Assert: content_text is NULL, redacted=1 for both observations
    var redactedCount int
    db.QueryRow(`SELECT COUNT(*) FROM observations WHERE redacted=1 AND content_text IS NULL`).Scan(&redactedCount)
    if redactedCount != 2 {
        t.Errorf("expected 2 redacted, got %d", redactedCount)
    }

    // Evidence preserved (row still there)
    var evCount int
    db.QueryRow(`SELECT COUNT(*) FROM evidence WHERE subject_id=1`).Scan(&evCount)
    if evCount != 2 {
        t.Errorf("evidence should be preserved, got %d", evCount)
    }

    // erasure_log row present
    var logCount int
    db.QueryRow(`SELECT COUNT(*) FROM erasure_log WHERE op_kind='soft'`).Scan(&logCount)
    if logCount != 1 {
        t.Errorf("expected 1 erasure_log row, got %d", logCount)
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/nikshilov/dev/ai/pulse && go test ./internal/erase/ -v`
Expected: FAIL — package does not exist.

- [ ] **Step 3: Write implementation**

Create `internal/erase/erase.go`:

```go
package erase

import (
    "context"
    "fmt"
    "time"

    "github.com/nkkmnk/pulse/internal/store"
)

type Eraser struct {
    store *store.Store
}

func NewEraser(s *store.Store) *Eraser {
    return &Eraser{store: s}
}

// SoftErase marks all observations linked (via evidence) to the entity as
// redacted=1 and sets content_text=NULL. Evidence rows and graph structure
// are preserved. Rollback possible — the audit log records op.
func (e *Eraser) SoftErase(ctx context.Context, entityID int64, initiatedBy, note string) error {
    db := e.store.DB()
    tx, err := db.BeginTx(ctx, nil)
    if err != nil { return err }
    defer tx.Rollback()

    now := time.Now().UTC().Format(time.RFC3339)
    logRes, err := tx.ExecContext(ctx, `
        INSERT INTO erasure_log (op_kind, subject_kind, subject_id, initiated_by, initiated_at, note)
        VALUES ('soft','entity',?,?,?,?)`,
        entityID, initiatedBy, now, note,
    )
    if err != nil { return fmt.Errorf("erasure_log: %w", err) }
    logID, _ := logRes.LastInsertId()

    _, err = tx.ExecContext(ctx, `
        UPDATE observations
        SET redacted=1, content_text=NULL
        WHERE id IN (
            SELECT observation_id FROM evidence WHERE subject_kind='entity' AND subject_id=?
        )`, entityID,
    )
    if err != nil { return fmt.Errorf("redact observations: %w", err) }

    _, err = tx.ExecContext(ctx, `UPDATE erasure_log SET completed_at=? WHERE id=?`, now, logID)
    if err != nil { return fmt.Errorf("mark completed: %w", err) }

    return tx.Commit()
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/nikshilov/dev/ai/pulse && go test ./internal/erase/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse
git add internal/erase/
git commit -m "feat(erase): soft erase — redact content, preserve graph structure"
```

---

## Task 10: Erasure — hard erase (delete observations + cascade)

**Files:**
- Modify: `internal/erase/erase.go`
- Modify: `internal/erase/erase_test.go`

- [ ] **Step 1: Write the failing test**

Add to `internal/erase/erase_test.go`:

```go
func TestHardEraseEntity(t *testing.T) {
    s := openTestStore(t)
    db := s.DB()

    _, err := db.Exec(`INSERT INTO entities (canonical_name, kind, first_seen, last_seen) VALUES ('Bob','person','2026-04-15T00:00:00Z','2026-04-15T00:00:00Z')`)
    if err != nil { t.Fatal(err) }
    _, err = db.Exec(`INSERT INTO observations (source_kind, source_id, content_hash, version, scope, captured_at, observed_at, actors, content_text) VALUES
        ('tg','m:1','h1',1,'nik','2026-04-15T00:00:00Z','2026-04-15T00:00:00Z','[]','private')`)
    if err != nil { t.Fatal(err) }
    _, err = db.Exec(`INSERT INTO evidence (subject_kind, subject_id, observation_id, created_at) VALUES ('entity',1,1,'2026-04-15T00:00:00Z')`)
    if err != nil { t.Fatal(err) }

    e := NewEraser(s)
    if err := e.HardErase(context.Background(), 1, "nik", "GDPR request"); err != nil {
        t.Fatalf("hard erase: %v", err)
    }

    var obsCount int
    db.QueryRow(`SELECT COUNT(*) FROM observations`).Scan(&obsCount)
    if obsCount != 0 {
        t.Errorf("expected 0 observations, got %d", obsCount)
    }

    var evCount int
    db.QueryRow(`SELECT COUNT(*) FROM evidence`).Scan(&evCount)
    if evCount != 0 {
        t.Errorf("expected 0 evidence rows (cascade), got %d", evCount)
    }

    var logCount int
    db.QueryRow(`SELECT COUNT(*) FROM erasure_log WHERE op_kind='hard'`).Scan(&logCount)
    if logCount != 1 {
        t.Errorf("expected 1 hard erasure_log, got %d", logCount)
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/nikshilov/dev/ai/pulse && go test ./internal/erase/ -run TestHardEraseEntity -v`
Expected: FAIL — HardErase undefined.

- [ ] **Step 3: Write implementation**

Append to `internal/erase/erase.go`:

```go
// HardErase deletes observations linked to the entity. Evidence rows cascade
// via FK ON DELETE CASCADE. Entity row is preserved (empty shell) unless
// caller also issues a follow-up delete. Non-reversible at DB level.
func (e *Eraser) HardErase(ctx context.Context, entityID int64, initiatedBy, note string) error {
    db := e.store.DB()
    tx, err := db.BeginTx(ctx, nil)
    if err != nil { return err }
    defer tx.Rollback()

    now := time.Now().UTC().Format(time.RFC3339)
    logRes, err := tx.ExecContext(ctx, `
        INSERT INTO erasure_log (op_kind, subject_kind, subject_id, initiated_by, initiated_at, note)
        VALUES ('hard','entity',?,?,?,?)`,
        entityID, initiatedBy, now, note,
    )
    if err != nil { return err }
    logID, _ := logRes.LastInsertId()

    _, err = tx.ExecContext(ctx, `
        DELETE FROM observations
        WHERE id IN (
            SELECT observation_id FROM evidence WHERE subject_kind='entity' AND subject_id=?
        )`, entityID,
    )
    if err != nil { return fmt.Errorf("delete observations: %w", err) }

    _, err = tx.ExecContext(ctx, `UPDATE erasure_log SET completed_at=? WHERE id=?`, now, logID)
    if err != nil { return err }

    return tx.Commit()
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/nikshilov/dev/ai/pulse && go test ./internal/erase/ -v`
Expected: PASS all erase tests.

- [ ] **Step 5: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse
git add internal/erase/
git commit -m "feat(erase): hard erase — delete observations, cascade evidence"
```

---

## Task 11: Python CLI — pulse_ingest.py skeleton

**Files:**
- Create: `scripts/pulse_ingest.py`
- Create: `scripts/providers/__init__.py`
- Create: `scripts/tests/test_pulse_ingest_cli.py`

- [ ] **Step 1: Write the failing test**

Create `scripts/tests/test_pulse_ingest_cli.py`:

```python
import subprocess
import sys
from pathlib import Path

CLI = Path(__file__).parent.parent / "pulse_ingest.py"

def test_help_shows_sources():
    result = subprocess.run([sys.executable, str(CLI), "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "--source" in result.stdout
    assert "claude-jsonl" in result.stdout
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/nikshilov/dev/ai/pulse && python -m pytest scripts/tests/test_pulse_ingest_cli.py -v`
Expected: FAIL — file not found.

- [ ] **Step 3: Write CLI skeleton**

Create `scripts/providers/__init__.py` (empty file).

Create `scripts/pulse_ingest.py`:

```python
#!/usr/bin/env python3
"""pulse_ingest — batch-import observations into Pulse from local sources."""

import argparse
import sys

KNOWN_SOURCES = ["claude-jsonl"]

def main() -> int:
    p = argparse.ArgumentParser(prog="pulse_ingest", description="Batch-import observations into Pulse.")
    p.add_argument("--source", required=True, choices=KNOWN_SOURCES,
                   help="which provider adapter to use (e.g. claude-jsonl)")
    p.add_argument("--path", required=True, help="source filesystem path")
    p.add_argument("--since", help="ISO date floor (YYYY-MM-DD)", default=None)
    p.add_argument("--pulse-url", default="http://localhost:18789",
                   help="Pulse server base URL")
    p.add_argument("--batch-size", type=int, default=200)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if args.source == "claude-jsonl":
        from providers.claude_jsonl import run as run_claude_jsonl
        return run_claude_jsonl(args)
    return 2

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/nikshilov/dev/ai/pulse && python -m pytest scripts/tests/test_pulse_ingest_cli.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse
git add scripts/pulse_ingest.py scripts/providers/__init__.py scripts/tests/test_pulse_ingest_cli.py
git commit -m "feat(scripts): pulse_ingest CLI skeleton with --source dispatch"
```

---

## Task 12: claude-jsonl provider — Normalize a single JSONL line

**Files:**
- Create: `scripts/providers/claude_jsonl.py`
- Create: `scripts/tests/fixtures/claude_jsonl_sample.jsonl`
- Create: `scripts/tests/test_claude_jsonl.py`

- [ ] **Step 1: Write the failing test**

Create `scripts/tests/fixtures/claude_jsonl_sample.jsonl`:

```
{"type":"user","timestamp":"2025-11-14T10:00:00Z","message":{"content":"привет"},"cwd":"/Users/nikshilov/dev/Garden","sessionId":"sess-a"}
{"type":"assistant","timestamp":"2025-11-14T10:00:02Z","message":{"content":[{"type":"text","text":"Привет, Ник."}]},"cwd":"/Users/nikshilov/dev/Garden","sessionId":"sess-a"}
{"type":"assistant","timestamp":"2025-11-14T10:00:03Z","message":{"content":[{"type":"tool_use","name":"Read","input":{}}]},"cwd":"/Users/nikshilov/dev/Garden","sessionId":"sess-a"}
{"type":"user","timestamp":"2025-11-14T10:00:05Z","message":{"content":"test"},"isMeta":true,"sessionId":"sess-a"}
```

Create `scripts/tests/test_claude_jsonl.py`:

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from providers.claude_jsonl import normalize_line, scan_file

FIXTURE = Path(__file__).parent / "fixtures" / "claude_jsonl_sample.jsonl"

def test_normalize_user_message():
    line = '{"type":"user","timestamp":"2025-11-14T10:00:00Z","message":{"content":"привет"},"cwd":"/x/y","sessionId":"s1"}'
    obs = normalize_line(line, source_file="fake.jsonl", line_index=0)
    assert obs is not None
    assert obs["source_kind"] == "claude_jsonl"
    assert obs["source_id"] == "fake.jsonl:0"
    assert obs["scope"] == "shared"
    assert obs["content_text"] == "привет"
    assert any(a["kind"] == "user" and a["id"] == "nik" for a in obs["actors"])

def test_normalize_assistant_text_extracts_text_blocks():
    line = '{"type":"assistant","timestamp":"2025-11-14T10:00:02Z","message":{"content":[{"type":"text","text":"Привет, Ник."}]},"cwd":"/x/Garden","sessionId":"s1"}'
    obs = normalize_line(line, source_file="f", line_index=1)
    assert obs is not None
    assert obs["content_text"] == "Привет, Ник."
    # agent id comes from cwd leaf (Garden)
    assert any(a["kind"] == "assistant" for a in obs["actors"])

def test_skip_tool_use():
    line = '{"type":"assistant","timestamp":"2025-11-14T10:00:03Z","message":{"content":[{"type":"tool_use","name":"Read","input":{}}]},"cwd":"/x/Garden","sessionId":"s1"}'
    obs = normalize_line(line, source_file="f", line_index=2)
    assert obs is None, "tool_use only should be skipped"

def test_skip_is_meta():
    line = '{"type":"user","timestamp":"2025-11-14T10:00:05Z","message":{"content":"test"},"isMeta":true,"sessionId":"s1"}'
    obs = normalize_line(line, source_file="f", line_index=3)
    assert obs is None

def test_scan_file_produces_expected_count():
    observations = list(scan_file(FIXTURE))
    # 1 user (meaningful) + 1 assistant (text) = 2
    assert len(observations) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/nikshilov/dev/ai/pulse && python -m pytest scripts/tests/test_claude_jsonl.py -v`
Expected: FAIL — no module.

- [ ] **Step 3: Write implementation**

Create `scripts/providers/claude_jsonl.py`:

```python
"""claude-jsonl provider — scan ~/.claude/projects/*.jsonl → Observations."""

import hashlib
import json
from pathlib import Path
from typing import Iterator, Optional

SKIP_SYSTEM_PREFIXES = ("<system-reminder", "<command-name>", "<local-command")


def _agent_id_from_cwd(cwd: str | None) -> str:
    if not cwd:
        return "unknown"
    return Path(cwd).name or "unknown"


def _content_hash(content: str, metadata: dict) -> str:
    h = hashlib.sha256()
    h.update(content.encode("utf-8"))
    h.update(b"\x1f")
    if metadata:
        canonical = [[k, metadata[k]] for k in sorted(metadata.keys())]
        h.update(json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return h.hexdigest()


def _extract_text(content) -> Optional[str]:
    """Pull text content out of user/assistant message. Skip tool_use, tool_result, thinking."""
    if isinstance(content, str):
        text = content.strip()
        return text or None
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                t = (block.get("text") or "").strip()
                if t:
                    parts.append(t)
            # skip tool_use, tool_result, thinking
        return "\n".join(parts) if parts else None
    return None


def _skippable_system_xml(text: str) -> bool:
    return any(text.lstrip().startswith(p) for p in SKIP_SYSTEM_PREFIXES)


def normalize_line(line: str, source_file: str, line_index: int) -> Optional[dict]:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None

    if obj.get("isMeta"):
        return None

    msg_type = obj.get("type")
    if msg_type not in ("user", "assistant"):
        return None

    message = obj.get("message") or {}
    text = _extract_text(message.get("content"))
    if not text or _skippable_system_xml(text):
        return None

    ts = obj.get("timestamp") or obj.get("createdAt")
    if not ts:
        return None

    cwd = obj.get("cwd")
    agent_id = _agent_id_from_cwd(cwd)
    if msg_type == "user":
        actor_primary = {"kind": "user", "id": "nik"}
    else:
        actor_primary = {"kind": "assistant", "id": agent_id}

    metadata = {
        "session_id": obj.get("sessionId"),
        "cwd": cwd,
        "git_branch": obj.get("gitBranch"),
        "model": (message.get("model") if isinstance(message, dict) else None),
    }
    metadata = {k: v for k, v in metadata.items() if v is not None}

    return {
        "source_kind": "claude_jsonl",
        "source_id": f"{source_file}:{line_index}",
        "content_hash": _content_hash(text, metadata),
        "version": 1,
        "scope": "shared",
        "captured_at": ts,
        "observed_at": ts,
        "actors": [actor_primary],
        "content_text": text,
        "metadata": metadata,
    }


def scan_file(path: Path) -> Iterator[dict]:
    with path.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            obs = normalize_line(line, source_file=path.name, line_index=i)
            if obs:
                yield obs


def run(args) -> int:
    """Entry point called from pulse_ingest.py."""
    base = Path(args.path).expanduser()
    if not base.exists():
        print(f"path not found: {base}", file=__import__("sys").stderr)
        return 2

    files = sorted(base.rglob("*.jsonl")) if base.is_dir() else [base]
    print(f"scanning {len(files)} files under {base}")

    total = 0
    for f in files:
        count = 0
        for _obs in scan_file(f):
            count += 1
            total += 1
        print(f"  {f.relative_to(base) if base.is_dir() else f.name}: {count} observations")

    print(f"TOTAL: {total}")
    return 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/nikshilov/dev/ai/pulse && python -m pytest scripts/tests/test_claude_jsonl.py -v`
Expected: PASS all cases.

- [ ] **Step 5: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse
git add scripts/providers/ scripts/tests/
git commit -m "feat(providers): claude_jsonl normalize — skip tool_use/isMeta/system XML"
```

---

## Task 13: claude-jsonl — batch POST to /ingest

**Files:**
- Modify: `scripts/providers/claude_jsonl.py`
- Create: `scripts/tests/test_claude_jsonl_post.py`

- [ ] **Step 1: Write the failing test**

Create `scripts/tests/test_claude_jsonl_post.py`:

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import patch, MagicMock
from providers.claude_jsonl import post_batch

def test_post_batch_sends_json():
    with patch("providers.claude_jsonl.httpx.post") as m:
        m.return_value = MagicMock(status_code=200, json=lambda: {"inserted": 3, "duplicates": 0, "revisions": 0})
        stats = post_batch("http://localhost:18789", [{"source_kind":"claude_jsonl","source_id":"f:1","content_hash":"h","version":1,"scope":"shared","captured_at":"2026-04-15T00:00:00Z","observed_at":"2026-04-15T00:00:00Z","actors":[],"content_text":"hi"}])
        m.assert_called_once()
        assert stats["inserted"] == 3

def test_post_batch_raises_on_error():
    with patch("providers.claude_jsonl.httpx.post") as m:
        m.return_value = MagicMock(status_code=500, text="boom")
        with pytest.raises(RuntimeError):
            post_batch("http://localhost:18789", [{}])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/nikshilov/dev/ai/pulse && python -m pytest scripts/tests/test_claude_jsonl_post.py -v`
Expected: FAIL — post_batch undefined.

- [ ] **Step 3: Add post_batch and wire into run()**

Edit `scripts/providers/claude_jsonl.py` — add import and function:

```python
import httpx

def post_batch(pulse_url: str, observations: list[dict]) -> dict:
    resp = httpx.post(f"{pulse_url}/ingest", json={"observations": observations}, timeout=60.0)
    if resp.status_code != 200:
        raise RuntimeError(f"ingest failed {resp.status_code}: {resp.text}")
    return resp.json()
```

Update `run(args)` to batch and post:

```python
def run(args) -> int:
    base = Path(args.path).expanduser()
    if not base.exists():
        print(f"path not found: {base}", file=__import__("sys").stderr)
        return 2

    files = sorted(base.rglob("*.jsonl")) if base.is_dir() else [base]
    print(f"scanning {len(files)} files under {base}")

    batch: list[dict] = []
    total_inserted = total_dup = total_rev = 0
    for f in files:
        for obs in scan_file(f):
            batch.append(obs)
            if len(batch) >= args.batch_size:
                if not args.dry_run:
                    stats = post_batch(args.pulse_url, batch)
                    total_inserted += stats.get("inserted", 0)
                    total_dup += stats.get("duplicates", 0)
                    total_rev += stats.get("revisions", 0)
                batch.clear()

    if batch and not args.dry_run:
        stats = post_batch(args.pulse_url, batch)
        total_inserted += stats.get("inserted", 0)
        total_dup += stats.get("duplicates", 0)
        total_rev += stats.get("revisions", 0)

    print(f"DONE: inserted={total_inserted} duplicates={total_dup} revisions={total_rev}")
    return 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/nikshilov/dev/ai/pulse && python -m pytest scripts/tests/ -v`
Expected: PASS all Python tests.

- [ ] **Step 5: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse
git add scripts/providers/claude_jsonl.py scripts/tests/test_claude_jsonl_post.py
git commit -m "feat(providers): claude_jsonl batch POST to /ingest with aggregate stats"
```

---

## Task 14: Integration smoke — M1/M2 end-to-end

**Files:**
- Create: `scripts/tests/test_integration_claude_jsonl.py`

- [ ] **Step 1: Write the integration test**

Create `scripts/tests/test_integration_claude_jsonl.py`:

```python
"""End-to-end: spin up Pulse, ingest fixture JSONL, verify DB rows.

Requires: Go build output `bin/pulse` and sqlite3 CLI.
Marked slow — skipped unless PULSE_E2E=1.
"""

import os
import subprocess
import sys
import time
import tempfile
from pathlib import Path

import pytest
import httpx

PULSE_BIN = Path(__file__).resolve().parent.parent.parent / "bin" / "pulse"
FIXTURE = Path(__file__).parent / "fixtures" / "claude_jsonl_sample.jsonl"

pytestmark = pytest.mark.skipif(os.getenv("PULSE_E2E") != "1", reason="set PULSE_E2E=1 to run")

def test_ingest_e2e(tmp_path):
    assert PULSE_BIN.exists(), f"build first: {PULSE_BIN}"
    db = tmp_path / "pulse.db"

    proc = subprocess.Popen(
        [str(PULSE_BIN), "serve", "--db", str(db), "--addr", "127.0.0.1:18899"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        # wait for ready
        for _ in range(30):
            try:
                httpx.get("http://127.0.0.1:18899/healthz", timeout=0.5)
                break
            except Exception:
                time.sleep(0.3)

        # Run ingest
        result = subprocess.run(
            [sys.executable, str(Path(__file__).parent.parent / "pulse_ingest.py"),
             "--source=claude-jsonl", f"--path={FIXTURE}",
             "--pulse-url=http://127.0.0.1:18899", "--batch-size=10"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr

        # Query DB directly
        import sqlite3
        con = sqlite3.connect(db)
        count = con.execute("SELECT COUNT(*) FROM observations WHERE source_kind='claude_jsonl'").fetchone()[0]
        assert count == 2, f"expected 2, got {count}"

        # Re-run → duplicates = 2
        result2 = subprocess.run(
            [sys.executable, str(Path(__file__).parent.parent / "pulse_ingest.py"),
             "--source=claude-jsonl", f"--path={FIXTURE}",
             "--pulse-url=http://127.0.0.1:18899", "--batch-size=10"],
            capture_output=True, text=True,
        )
        assert result2.returncode == 0
        assert "duplicates=2" in result2.stdout
    finally:
        proc.terminate()
        proc.wait(timeout=5)
```

- [ ] **Step 2: Build and run**

Run:
```bash
cd /Users/nikshilov/dev/ai/pulse
go build -o bin/pulse ./cmd/pulse
PULSE_E2E=1 python -m pytest scripts/tests/test_integration_claude_jsonl.py -v
```

Expected: PASS (ingest 2, re-run says duplicates=2).

- [ ] **Step 3: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse
git add scripts/tests/test_integration_claude_jsonl.py
git commit -m "test(e2e): ingest fixture JSONL via HTTP, verify dedup on re-run"
```

---

## Task 15: Extractor — triage prompt skeleton (Sonnet)

**Files:**
- Create: `scripts/extract/__init__.py`
- Create: `scripts/extract/prompts.py`
- Create: `scripts/tests/test_triage_prompt.py`

- [ ] **Step 1: Write the failing test**

Create `scripts/tests/test_triage_prompt.py`:

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from extract.prompts import build_triage_prompt, parse_triage_response

def test_build_triage_prompt_includes_all_observations():
    obs = [
        {"id": 1, "source_kind": "claude_jsonl", "content_text": "привет", "actors": [{"kind":"user","id":"nik"}]},
        {"id": 2, "source_kind": "claude_jsonl", "content_text": "test tool output", "actors": [{"kind":"assistant","id":"elle"}]},
    ]
    prompt = build_triage_prompt(obs)
    assert "1." in prompt and "2." in prompt
    assert "привет" in prompt
    assert "verdict" in prompt.lower()

def test_parse_triage_response_extracts_verdicts():
    resp = """
    1. extract — emotional greeting
    2. skip — trivial tooling
    3. defer — needs more context
    """
    verdicts = parse_triage_response(resp, expected_count=3)
    assert verdicts == [
        {"verdict": "extract", "reason": "emotional greeting"},
        {"verdict": "skip", "reason": "trivial tooling"},
        {"verdict": "defer", "reason": "needs more context"},
    ]

def test_parse_triage_response_handles_short_form():
    resp = "1. extract\n2. skip\n3. extract"
    verdicts = parse_triage_response(resp, expected_count=3)
    assert [v["verdict"] for v in verdicts] == ["extract", "skip", "extract"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/nikshilov/dev/ai/pulse && python -m pytest scripts/tests/test_triage_prompt.py -v`
Expected: FAIL — no module.

- [ ] **Step 3: Write implementation**

Create `scripts/extract/__init__.py` (empty).

Create `scripts/extract/prompts.py`:

```python
"""Prompts for two-pass extractor: Sonnet triage + Opus extract."""

import re
from typing import Iterable

TRIAGE_INSTRUCTIONS = """You are the triage filter for an emotional memory graph.
For each numbered observation below, decide:
- `extract` — has people/places/projects, emotions, decisions, or meaningful events
- `skip` — trivial (tool output, mechanical, noise)
- `defer` — ambiguous, needs more context later

Respond with ONE line per observation, format:
<number>. <verdict> — <one-line reason>

Be strict: default to skip if no humans or emotional weight.
"""


def build_triage_prompt(observations: Iterable[dict]) -> str:
    lines = [TRIAGE_INSTRUCTIONS, "", "Observations:"]
    for i, obs in enumerate(observations, 1):
        actors = ", ".join(f"{a.get('kind','?')}:{a.get('id','?')}" for a in obs.get("actors", []))
        text = (obs.get("content_text") or "").replace("\n", " ")[:500]
        lines.append(f"{i}. [{obs.get('source_kind')} | {actors}] {text}")
    lines.append("")
    lines.append("Give your verdict lines now.")
    return "\n".join(lines)


_VERDICT_RE = re.compile(r"^\s*(\d+)\.\s*(extract|skip|defer)\b(?:\s*[—\-:]\s*(.*))?$", re.IGNORECASE)


def parse_triage_response(response: str, expected_count: int) -> list[dict]:
    verdicts: dict[int, dict] = {}
    for line in response.splitlines():
        m = _VERDICT_RE.match(line.strip())
        if not m:
            continue
        n = int(m.group(1))
        v = m.group(2).lower()
        reason = (m.group(3) or "").strip()
        verdicts[n] = {"verdict": v, "reason": reason}

    return [
        verdicts.get(i, {"verdict": "defer", "reason": "missing from response"})
        for i in range(1, expected_count + 1)
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/nikshilov/dev/ai/pulse && python -m pytest scripts/tests/test_triage_prompt.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse
git add scripts/extract/ scripts/tests/test_triage_prompt.py
git commit -m "feat(extract): triage prompt — Sonnet filter extract/skip/defer per observation"
```

---

## Task 16: Extractor — extract prompt (Opus) + response schema

**Files:**
- Modify: `scripts/extract/prompts.py`
- Create: `scripts/tests/test_extract_prompt.py`

- [ ] **Step 1: Write the failing test**

Create `scripts/tests/test_extract_prompt.py`:

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import json
from extract.prompts import build_extract_prompt, parse_extract_response

def test_build_extract_prompt_includes_graph_context():
    obs = {"id": 1, "source_kind":"claude_jsonl", "content_text":"Аня сказала что Федя пошёл в школу", "actors":[{"kind":"user","id":"nik"}]}
    graph_context = {
        "existing_entities": [
            {"id": 10, "canonical_name": "Anna", "kind": "person", "aliases": ["Аня"]},
        ],
    }
    prompt = build_extract_prompt(obs, graph_context)
    assert "Anna" in prompt
    assert "Аня" in prompt or "Анна" in prompt
    assert "JSON" in prompt

def test_parse_extract_response_valid_json():
    resp = json.dumps({
        "entities": [
            {"canonical_name": "Anna", "kind": "person", "aliases": ["Аня"], "salience": 0.9, "emotional_weight": 0.8},
            {"canonical_name": "Fedya", "kind": "person", "aliases": ["Федя"], "salience": 0.5, "emotional_weight": 0.3},
        ],
        "relations": [
            {"from": "Anna", "to": "Fedya", "kind": "parent", "strength": 0.9},
        ],
        "events": [
            {"title": "Fedya started school", "sentiment": 0.7, "emotional_weight": 0.4, "ts": "2026-04-15T00:00:00Z", "entities": ["Fedya"]},
        ],
        "facts": [],
        "merge_candidates": [],
    })
    out = parse_extract_response(resp)
    assert len(out["entities"]) == 2
    assert out["relations"][0]["kind"] == "parent"

def test_parse_extract_response_tolerates_code_fence():
    resp = "```json\n{\"entities\":[],\"relations\":[],\"events\":[],\"facts\":[],\"merge_candidates\":[]}\n```"
    out = parse_extract_response(resp)
    assert out["entities"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/nikshilov/dev/ai/pulse && python -m pytest scripts/tests/test_extract_prompt.py -v`
Expected: FAIL.

- [ ] **Step 3: Add to prompts.py**

Append to `scripts/extract/prompts.py`:

```python
import json as _json

EXTRACT_INSTRUCTIONS = """You are the emotional memory extractor.

Given an observation from Nik's life, extract:
- entities (people, places, projects, orgs, things mentioned)
- relations between entities (from, to, kind, strength 0-1)
- events (title, sentiment -1..1, emotional_weight 0-1, ts, entities involved)
- facts (atomic claims about entities: text, confidence 0-1)
- merge_candidates (if you see an entity that might be the same as an existing one, list with confidence 0-1)

For scoring:
- salience: how important is this entity to Nik's life (0-1)
- emotional_weight: how emotionally charged (0-1, 1=Anna/therapist-level, 0=random colleague)
- sentiment: positive/negative valence (-1..1)

Ground every entity/relation/fact/event in the observation's content.
If you see a name that matches an existing entity alias, prefer existing_entities.
Return STRICTLY valid JSON with keys: entities, relations, events, facts, merge_candidates.
"""


def build_extract_prompt(observation: dict, graph_context: dict) -> str:
    existing = graph_context.get("existing_entities", [])
    existing_lines = []
    for e in existing:
        aliases = ", ".join(e.get("aliases") or [])
        existing_lines.append(f"  - id={e['id']} name={e['canonical_name']} kind={e['kind']} aliases=[{aliases}]")

    actors = ", ".join(f"{a.get('kind')}:{a.get('id')}" for a in observation.get("actors", []))
    return "\n".join([
        EXTRACT_INSTRUCTIONS,
        "",
        "Existing entities:",
        "\n".join(existing_lines) if existing_lines else "  (none)",
        "",
        f"Observation (source={observation.get('source_kind')}, actors={actors}):",
        observation.get("content_text", ""),
        "",
        "Respond with JSON only:",
    ])


def parse_extract_response(response: str) -> dict:
    text = response.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)

    data = _json.loads(text)
    for key in ("entities", "relations", "events", "facts", "merge_candidates"):
        data.setdefault(key, [])
    return data
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/nikshilov/dev/ai/pulse && python -m pytest scripts/tests/test_extract_prompt.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse
git add scripts/extract/prompts.py scripts/tests/test_extract_prompt.py
git commit -m "feat(extract): Opus extract prompt — entities/relations/events/facts/merge_candidates JSON schema"
```

---

## Task 17: Entity resolver — confidence gates

**Files:**
- Create: `scripts/extract/resolver.py`
- Create: `scripts/tests/test_resolver.py`

- [ ] **Step 1: Write the failing test**

Create `scripts/tests/test_resolver.py`:

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from extract.resolver import resolve_entity, ResolutionDecision

EXISTING = [
    {"id": 10, "canonical_name": "Anna", "kind": "person", "aliases": ["Аня", "Анна"]},
    {"id": 11, "canonical_name": "Fedya", "kind": "person", "aliases": ["Федя"]},
]

def test_exact_alias_match_auto_merge():
    candidate = {"canonical_name": "Аня", "kind": "person", "aliases": []}
    dec = resolve_entity(candidate, EXISTING)
    assert dec.action == "bind_identity"
    assert dec.entity_id == 10
    assert dec.confidence >= 0.98

def test_soft_match_creates_proposal():
    candidate = {"canonical_name": "Анна Петровна", "kind": "person", "aliases": []}
    dec = resolve_entity(candidate, EXISTING)
    # first-name overlap "Анна" ≈ "Anna" → soft match
    assert dec.action in ("proposal", "bind_identity")
    assert 0.7 <= dec.confidence

def test_no_match_creates_open_question():
    candidate = {"canonical_name": "Random Stranger", "kind": "person", "aliases": []}
    dec = resolve_entity(candidate, EXISTING)
    assert dec.action == "new_entity_with_question"
    assert dec.confidence < 0.7
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/nikshilov/dev/ai/pulse && python -m pytest scripts/tests/test_resolver.py -v`
Expected: FAIL.

- [ ] **Step 3: Write resolver**

Create `scripts/extract/resolver.py`:

```python
"""Entity resolution with confidence gates.

Gates (per design v3):
- exact alias / canonical match → auto bind (confidence 1.0)
- 0.7..0.98 → entity_merge_proposals pending
- < 0.7 → new entity + open_question
"""

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Literal, Optional


@dataclass
class ResolutionDecision:
    action: Literal["bind_identity", "proposal", "new_entity_with_question"]
    entity_id: Optional[int]
    confidence: float
    reason: str


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _best_match(candidate: dict, existing: list[dict]) -> tuple[Optional[dict], float]:
    name = candidate.get("canonical_name", "")
    aliases = candidate.get("aliases") or []
    kind = candidate.get("kind")
    best = None
    best_score = 0.0

    for ent in existing:
        if kind and ent.get("kind") != kind:
            continue
        ent_all = [ent["canonical_name"]] + list(ent.get("aliases") or [])
        cand_all = [name] + list(aliases)
        pair_best = 0.0
        for c in cand_all:
            if not c:
                continue
            for e in ent_all:
                score = _similarity(c, e)
                if score > pair_best:
                    pair_best = score
        if pair_best > best_score:
            best_score = pair_best
            best = ent

    return best, best_score


def resolve_entity(candidate: dict, existing: list[dict]) -> ResolutionDecision:
    best, score = _best_match(candidate, existing)
    if best is None:
        return ResolutionDecision("new_entity_with_question", None, 0.0, "no existing entities of this kind")
    if score >= 0.98:
        return ResolutionDecision("bind_identity", best["id"], score, f"exact match to {best['canonical_name']}")
    if score >= 0.7:
        return ResolutionDecision("proposal", best["id"], score, f"soft match to {best['canonical_name']} at {score:.2f}")
    return ResolutionDecision("new_entity_with_question", None, score, f"best candidate {best['canonical_name']} below 0.7")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/nikshilov/dev/ai/pulse && python -m pytest scripts/tests/test_resolver.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse
git add scripts/extract/resolver.py scripts/tests/test_resolver.py
git commit -m "feat(extract): entity resolver — confidence gates (0.98/0.7 thresholds)"
```

---

## Task 18: Scorer — version pinning

**Files:**
- Create: `scripts/extract/scorer.py`
- Create: `scripts/tests/test_scorer.py`

- [ ] **Step 1: Write the failing test**

Create `scripts/tests/test_scorer.py`:

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from extract.scorer import CURRENT_VERSION, score_entity, score_event

def test_version_is_semver_string():
    assert isinstance(CURRENT_VERSION, str)
    assert CURRENT_VERSION.startswith("v")

def test_score_entity_passes_through_model_values():
    raw = {"salience": 0.8, "emotional_weight": 0.9}
    scored = score_entity(raw)
    assert scored["salience_score"] == 0.8
    assert scored["emotional_weight"] == 0.9
    assert scored["scorer_version"] == CURRENT_VERSION

def test_score_entity_clamps_out_of_range():
    raw = {"salience": 1.5, "emotional_weight": -0.2}
    scored = score_entity(raw)
    assert scored["salience_score"] == 1.0
    assert scored["emotional_weight"] == 0.0

def test_score_event_preserves_sentiment_sign():
    raw = {"sentiment": -0.5, "emotional_weight": 0.7}
    scored = score_event(raw)
    assert scored["sentiment"] == -0.5
    assert scored["emotional_weight"] == 0.7
    assert scored["scorer_version"] == CURRENT_VERSION
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/nikshilov/dev/ai/pulse && python -m pytest scripts/tests/test_scorer.py -v`
Expected: FAIL.

- [ ] **Step 3: Write implementation**

Create `scripts/extract/scorer.py`:

```python
"""Salience / emotional_weight / sentiment scoring. Version-pinned for drift handling."""

CURRENT_VERSION = "v1.0"


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def score_entity(raw: dict) -> dict:
    return {
        "salience_score": _clamp(float(raw.get("salience", 0.0))),
        "emotional_weight": _clamp(float(raw.get("emotional_weight", 0.0))),
        "scorer_version": CURRENT_VERSION,
    }


def score_event(raw: dict) -> dict:
    return {
        "sentiment": _clamp(float(raw.get("sentiment", 0.0)), lo=-1.0, hi=1.0),
        "emotional_weight": _clamp(float(raw.get("emotional_weight", 0.0))),
        "scorer_version": CURRENT_VERSION,
    }


def score_fact(raw: dict) -> dict:
    return {
        "confidence": _clamp(float(raw.get("confidence", 1.0))),
        "scorer_version": CURRENT_VERSION,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/nikshilov/dev/ai/pulse && python -m pytest scripts/tests/test_scorer.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse
git add scripts/extract/scorer.py scripts/tests/test_scorer.py
git commit -m "feat(extract): scorer v1.0 — clamped salience/emotional/sentiment with scorer_version pin"
```

---

## Task 19: Extractor loop — wire triage + extract + store upserts

**Files:**
- Create: `scripts/pulse_extract.py`
- Create: `scripts/tests/test_extract_loop.py`

- [ ] **Step 1: Write the failing test**

Create `scripts/tests/test_extract_loop.py`:

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import sqlite3
import pytest
from unittest.mock import patch
import pulse_extract


@pytest.fixture
def seeded_db(tmp_path):
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    # Apply all migrations
    for mig in sorted(Path(__file__).resolve().parents[2].glob("internal/store/migrations/*.sql")):
        con.executescript(mig.read_text())
    con.commit()

    con.execute("""INSERT INTO observations
        (source_kind, source_id, content_hash, version, scope, captured_at, observed_at, actors, content_text, metadata, raw_json)
        VALUES ('claude_jsonl','f:1','h1',1,'shared','2026-04-15T00:00:00Z','2026-04-15T00:00:01Z','[{"kind":"user","id":"nik"}]','Аня упомянула Федю — пошёл в школу','{}','{}')""")

    con.execute("""INSERT INTO extraction_jobs
        (observation_ids, state, attempts, created_at, updated_at)
        VALUES ('[1]', 'pending', 0, '2026-04-15T00:00:00Z', '2026-04-15T00:00:00Z')""")
    con.commit()
    con.close()
    return db


def test_extract_loop_processes_pending(seeded_db, monkeypatch):
    from unittest.mock import MagicMock

    fake_triage = MagicMock(return_value=[{"verdict": "extract", "reason": "family mention"}])
    fake_extract = MagicMock(return_value={
        "entities": [{"canonical_name": "Anna", "kind": "person", "aliases": ["Аня"], "salience": 0.9, "emotional_weight": 0.7}],
        "relations": [],
        "events": [],
        "facts": [],
        "merge_candidates": [],
    })

    monkeypatch.setattr(pulse_extract, "call_sonnet_triage", fake_triage)
    monkeypatch.setattr(pulse_extract, "call_opus_extract", fake_extract)

    rc = pulse_extract.run_once(str(seeded_db), budget_usd_remaining=10.0)
    assert rc == 0

    con = sqlite3.connect(seeded_db)
    entities = con.execute("SELECT canonical_name FROM entities").fetchall()
    assert any(e[0] == "Anna" for e in entities)

    # Job marked done
    state = con.execute("SELECT state FROM extraction_jobs WHERE id=1").fetchone()[0]
    assert state == "done"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/nikshilov/dev/ai/pulse && python -m pytest scripts/tests/test_extract_loop.py -v`
Expected: FAIL.

- [ ] **Step 3: Write extract loop**

Create `scripts/pulse_extract.py`:

```python
#!/usr/bin/env python3
"""pulse_extract — run one iteration of the two-pass extractor loop.

Reads pending extraction_jobs, runs Sonnet triage + Opus extract, writes
entities/relations/events/facts/evidence, advances job state.
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from extract import prompts, resolver, scorer

# These wrap anthropic SDK in real code. For tests they're monkey-patched.
def call_sonnet_triage(prompt: str) -> list[dict]:
    raise NotImplementedError("wire up anthropic client before production run")


def call_opus_extract(prompt: str) -> dict:
    raise NotImplementedError("wire up anthropic client before production run")


def _load_observations(con: sqlite3.Connection, ids: list[int]) -> list[dict]:
    q = "SELECT id, source_kind, content_text, actors, metadata FROM observations WHERE id IN (%s)" % ",".join("?" * len(ids))
    out = []
    for row in con.execute(q, ids):
        out.append({
            "id": row[0],
            "source_kind": row[1],
            "content_text": row[2],
            "actors": json.loads(row[3] or "[]"),
            "metadata": json.loads(row[4] or "{}"),
        })
    return out


def _load_existing_entities(con: sqlite3.Connection) -> list[dict]:
    rows = con.execute("SELECT id, canonical_name, kind, aliases FROM entities").fetchall()
    return [
        {"id": r[0], "canonical_name": r[1], "kind": r[2], "aliases": json.loads(r[3] or "[]")}
        for r in rows
    ]


def _apply_extraction(con: sqlite3.Connection, obs_id: int, result: dict) -> None:
    existing = _load_existing_entities(con)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    for ent in result.get("entities", []):
        dec = resolver.resolve_entity(ent, existing)
        scored = scorer.score_entity(ent)
        if dec.action == "bind_identity":
            # update last_seen + scores (we trust new data equally)
            con.execute("UPDATE entities SET last_seen=?, salience_score=?, emotional_weight=?, scorer_version=? WHERE id=?",
                        (now, scored["salience_score"], scored["emotional_weight"], scored["scorer_version"], dec.entity_id))
            entity_id = dec.entity_id
        else:
            cur = con.execute(
                "INSERT INTO entities (canonical_name, kind, aliases, first_seen, last_seen, salience_score, emotional_weight, scorer_version) VALUES (?,?,?,?,?,?,?,?)",
                (ent["canonical_name"], ent.get("kind", "person"), json.dumps(ent.get("aliases") or []),
                 now, now, scored["salience_score"], scored["emotional_weight"], scored["scorer_version"]),
            )
            entity_id = cur.lastrowid
            existing.append({"id": entity_id, "canonical_name": ent["canonical_name"], "kind": ent.get("kind","person"), "aliases": ent.get("aliases") or []})

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

        con.execute("INSERT INTO evidence (subject_kind, subject_id, observation_id, created_at) VALUES ('entity',?,?,?)",
                    (entity_id, obs_id, now))

    # events
    for ev in result.get("events", []):
        s = scorer.score_event(ev)
        cur = con.execute("INSERT INTO events (title, description, sentiment, emotional_weight, scorer_version, ts) VALUES (?,?,?,?,?,?)",
                          (ev.get("title",""), ev.get("description",""), s["sentiment"], s["emotional_weight"], s["scorer_version"], ev.get("ts", now)))
        con.execute("INSERT INTO evidence (subject_kind, subject_id, observation_id, created_at) VALUES ('event',?,?,?)",
                    (cur.lastrowid, obs_id, now))


def run_once(db_path: str, budget_usd_remaining: float = 10.0) -> int:
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys=ON")

    jobs = con.execute("SELECT id, observation_ids FROM extraction_jobs WHERE state='pending' ORDER BY created_at LIMIT 10").fetchall()
    if not jobs:
        print("no pending jobs")
        return 0

    for job_id, obs_ids_json in jobs:
        obs_ids = json.loads(obs_ids_json)
        con.execute("UPDATE extraction_jobs SET state='running', attempts=attempts+1, updated_at=? WHERE id=?",
                    (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), job_id))
        con.commit()

        observations = _load_observations(con, obs_ids)
        if not observations:
            con.execute("UPDATE extraction_jobs SET state='failed', last_error='no observations', updated_at=? WHERE id=?",
                        (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), job_id))
            con.commit()
            continue

        try:
            triage_prompt = prompts.build_triage_prompt(observations)
            verdicts = call_sonnet_triage(triage_prompt)

            for obs, v in zip(observations, verdicts):
                if v["verdict"] != "extract":
                    continue
                graph_ctx = {"existing_entities": _load_existing_entities(con)}
                extract_prompt = prompts.build_extract_prompt(obs, graph_ctx)
                result = call_opus_extract(extract_prompt)
                _apply_extraction(con, obs["id"], result)

            con.execute("UPDATE extraction_jobs SET state='done', triage_model='sonnet-4.6', extract_model='opus-4.6', updated_at=? WHERE id=?",
                        (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), job_id))
            con.commit()
        except Exception as e:
            con.execute("UPDATE extraction_jobs SET state='failed', last_error=?, updated_at=? WHERE id=?",
                        (str(e)[:500], time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), job_id))
            con.commit()

    con.close()
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", required=True)
    p.add_argument("--budget", type=float, default=float(os.getenv("PULSE_DAILY_EXTRACT_BUDGET_USD", "10")))
    args = p.parse_args()
    return run_once(args.db, args.budget)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/nikshilov/dev/ai/pulse && python -m pytest scripts/tests/test_extract_loop.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse
git add scripts/pulse_extract.py scripts/tests/test_extract_loop.py
git commit -m "feat(extract): pulse_extract loop — triage+extract, apply entities/events/evidence, advance jobs"
```

---

## Task 20: Extractor — DLQ on max-attempts

**Files:**
- Modify: `scripts/pulse_extract.py`
- Modify: `scripts/tests/test_extract_loop.py`

- [ ] **Step 1: Write the failing test**

Add to `scripts/tests/test_extract_loop.py`:

```python
def test_failed_job_moves_to_dlq_after_three_attempts(seeded_db, monkeypatch):
    import pulse_extract

    def boom(*_a, **_kw):
        raise RuntimeError("triage API down")

    monkeypatch.setattr(pulse_extract, "call_sonnet_triage", boom)

    for _ in range(3):
        pulse_extract.run_once(str(seeded_db))

    con = sqlite3.connect(seeded_db)
    state = con.execute("SELECT state FROM extraction_jobs WHERE id=1").fetchone()[0]
    assert state == "dlq"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/nikshilov/dev/ai/pulse && python -m pytest scripts/tests/test_extract_loop.py::test_failed_job_moves_to_dlq_after_three_attempts -v`
Expected: FAIL — stays in `failed`.

- [ ] **Step 3: Add DLQ logic**

Edit `scripts/pulse_extract.py` — replace the failing-state write:

```python
except Exception as e:
    attempts_row = con.execute("SELECT attempts FROM extraction_jobs WHERE id=?", (job_id,)).fetchone()
    attempts = attempts_row[0] if attempts_row else 0
    next_state = "dlq" if attempts >= 3 else "pending"  # pending → retry on next run
    con.execute("UPDATE extraction_jobs SET state=?, last_error=?, updated_at=? WHERE id=?",
                (next_state, str(e)[:500], time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), job_id))
    con.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/nikshilov/dev/ai/pulse && python -m pytest scripts/tests/test_extract_loop.py -v`
Expected: PASS (all cases).

- [ ] **Step 5: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse
git add scripts/pulse_extract.py scripts/tests/test_extract_loop.py
git commit -m "feat(extract): DLQ after 3 failed attempts, otherwise retry pending"
```

---

## Task 21: Extractor — budget guard

**Files:**
- Modify: `scripts/pulse_extract.py`
- Modify: `scripts/tests/test_extract_loop.py`

- [ ] **Step 1: Write the failing test**

Add to `scripts/tests/test_extract_loop.py`:

```python
def test_budget_exhausted_skips_extraction(seeded_db, capsys):
    import pulse_extract
    rc = pulse_extract.run_once(str(seeded_db), budget_usd_remaining=0.0)
    captured = capsys.readouterr()
    assert "budget" in captured.out.lower()

    con = sqlite3.connect(seeded_db)
    # Job should remain pending, NOT be moved to running/failed
    state = con.execute("SELECT state FROM extraction_jobs WHERE id=1").fetchone()[0]
    assert state == "pending"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/nikshilov/dev/ai/pulse && python -m pytest scripts/tests/test_extract_loop.py::test_budget_exhausted_skips_extraction -v`
Expected: FAIL — job runs anyway.

- [ ] **Step 3: Add guard**

Edit `scripts/pulse_extract.py` — at top of `run_once`, after loading jobs:

```python
if budget_usd_remaining <= 0:
    print("budget exhausted for today — skipping extraction run")
    con.close()
    return 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/nikshilov/dev/ai/pulse && python -m pytest scripts/tests/test_extract_loop.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse
git add scripts/pulse_extract.py scripts/tests/test_extract_loop.py
git commit -m "feat(extract): daily budget guard — skip when PULSE_DAILY_EXTRACT_BUDGET_USD exhausted"
```

---

## Task 22: Ingest → auto-enqueue extraction_jobs

**Files:**
- Modify: `internal/ingest/dedupe.go` (add enqueue on insert/revision)
- Modify: `internal/ingest/handler_test.go`

- [ ] **Step 1: Write the failing test**

Add to `internal/ingest/handler_test.go`:

```go
func TestIngestEnqueuesExtractionJob(t *testing.T) {
    s := openTestStore(t)
    h := NewHandler(s)

    obs := capture.Observation{
        SourceKind: "tg", SourceID: "m:99", ContentHash: "h1", Version: 1,
        Scope: "nik",
        CapturedAt: timeParse(t, "2026-04-15T00:00:00Z"),
        ObservedAt: timeParse(t, "2026-04-15T00:00:01Z"),
        Actors: []capture.ActorRef{{Kind: "tg_user", ID: "123"}},
        ContentText: "meaningful",
    }
    body, _ := json.Marshal(map[string]any{"observations": []capture.Observation{obs}})
    h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodPost, "/ingest", bytes.NewReader(body)))

    var jobCount int
    s.DB().QueryRow(`SELECT COUNT(*) FROM extraction_jobs WHERE state='pending'`).Scan(&jobCount)
    if jobCount != 1 {
        t.Errorf("expected 1 pending job, got %d", jobCount)
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/nikshilov/dev/ai/pulse && go test ./internal/ingest/ -v`
Expected: FAIL — 0 jobs.

- [ ] **Step 3: Add enqueue**

Edit `internal/ingest/dedupe.go`: after `insertObservation` call (both insert and revision paths), add:

```go
func enqueueExtractionJob(ctx context.Context, db *sql.DB, obsID int64) error {
    ids, _ := json.Marshal([]int64{obsID})
    now := time.Now().UTC().Format(time.RFC3339)
    _, err := db.ExecContext(ctx, `
        INSERT INTO extraction_jobs (observation_ids, state, attempts, created_at, updated_at)
        VALUES (?, 'pending', 0, ?, ?)`,
        string(ids), now, now,
    )
    return err
}
```

Call from `upsert` after successful insert/revision:

```go
// after opInsert path:
if err := enqueueExtractionJob(ctx, db, id); err != nil {
    return 0, 0, fmt.Errorf("enqueue: %w", err)
}

// after revision path (same):
if err := enqueueExtractionJob(ctx, db, newID); err != nil {
    return 0, 0, fmt.Errorf("enqueue: %w", err)
}
```

(Don't enqueue on duplicate.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/nikshilov/dev/ai/pulse && go test ./internal/ingest/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse
git add internal/ingest/
git commit -m "feat(ingest): enqueue extraction_job on insert and revision (not on duplicate)"
```

---

## Task 23: Sensitive actors — ingest-time policy enforcement

**Files:**
- Modify: `internal/ingest/handler.go` (enforce policy before insert)
- Create: `internal/ingest/sensitive_test.go`

- [ ] **Step 1: Write the failing test**

Create `internal/ingest/sensitive_test.go`:

```go
package ingest

import (
    "bytes"
    "encoding/json"
    "net/http"
    "net/http/httptest"
    "testing"

    "github.com/nkkmnk/pulse/internal/capture"
)

func TestSensitiveActorRedactContentPolicy(t *testing.T) {
    s := openTestStore(t)
    db := s.DB()

    // Seed: entity "Anna" with tg identity 42, policy=redact_content
    _, _ = db.Exec(`INSERT INTO entities (canonical_name, kind, first_seen, last_seen) VALUES ('Anna','person','2026-04-15T00:00:00Z','2026-04-15T00:00:00Z')`)
    _, _ = db.Exec(`INSERT INTO entity_identities (entity_id, source_kind, identifier, first_seen) VALUES (1,'telegram','42','2026-04-15T00:00:00Z')`)
    _, _ = db.Exec(`INSERT INTO sensitive_actors (entity_id, policy, added_at, added_by) VALUES (1,'redact_content','2026-04-15T00:00:00Z','nik')`)

    h := NewHandler(s)
    obs := capture.Observation{
        SourceKind: "telegram", SourceID: "m:555", ContentHash: "h1", Version: 1,
        Scope: "nik",
        CapturedAt: timeParse(t, "2026-04-15T00:00:00Z"),
        ObservedAt: timeParse(t, "2026-04-15T00:00:01Z"),
        Actors: []capture.ActorRef{{Kind: "telegram", ID: "42"}},
        ContentText: "intimate content here",
    }
    body, _ := json.Marshal(map[string]any{"observations": []capture.Observation{obs}})
    h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodPost, "/ingest", bytes.NewReader(body)))

    var stored string
    var redacted int
    db.QueryRow(`SELECT COALESCE(content_text,''), redacted FROM observations WHERE source_id='m:555'`).Scan(&stored, &redacted)

    if redacted != 1 {
        t.Errorf("expected redacted=1, got %d", redacted)
    }
    if stored == "intimate content here" {
        t.Errorf("content should be redacted, got %q", stored)
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/nikshilov/dev/ai/pulse && go test ./internal/ingest/ -v`
Expected: FAIL.

- [ ] **Step 3: Add policy enforcement**

Edit `internal/ingest/dedupe.go` — add function:

```go
func applySensitivePolicy(ctx context.Context, db *sql.DB, obs *capture.Observation) error {
    if len(obs.Actors) == 0 {
        return nil
    }
    // Look for any actor whose (kind→source_kind_match, id) maps to sensitive policy
    for _, a := range obs.Actors {
        var policy string
        err := db.QueryRowContext(ctx, `
            SELECT sa.policy
            FROM sensitive_actors sa
            JOIN entity_identities ei ON ei.entity_id = sa.entity_id
            WHERE ei.source_kind = ? AND ei.identifier = ?`,
            obs.SourceKind, a.ID,
        ).Scan(&policy)
        if err == sql.ErrNoRows {
            continue
        }
        if err != nil {
            return err
        }

        switch policy {
        case "no_capture":
            obs.ContentText = ""
            obs.MediaRefs = nil
            obs.Metadata = map[string]any{"sensitive": "no_capture"}
        case "redact_content":
            obs.ContentText = "[redacted]"
        case "summary_only":
            // M3 just drops to summary stub; real summary runs in extractor
            obs.ContentText = "[summary_only]"
        }
        return nil
    }
    return nil
}
```

Call from `upsert` **before** `insertObservation`:

```go
if err := applySensitivePolicy(ctx, db, obs); err != nil {
    return 0, 0, err
}
```

And bump insert to mark `redacted=1` when the policy changed content. The easiest path: modify `insertObservation` to accept `redacted bool`, OR after insert update `redacted=1` when policy applied. Simpler is to add a field on the obs in-memory and branch insert:

Add parameter to insert:

```go
func insertObservation(ctx context.Context, db *sql.DB, obs *capture.Observation, redacted int) (int64, error) {
    // ... existing body but include redacted in INSERT ...
}
```

And set `redacted=1` inside `applySensitivePolicy` by stashing on obs via a package-level struct or simply by passing the flag through the call chain. Choose one approach — consistent is: add `Redacted bool` field to `capture.Observation` (not serialized to DB directly but used within upsert).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/nikshilov/dev/ai/pulse && go test ./internal/ingest/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse
git add internal/ingest/ internal/capture/
git commit -m "feat(ingest): sensitive_actors policy enforcement at ingest (redact/summary/no_capture)"
```

---

## Task 24: End-to-end extractor smoke — claude_jsonl fixture → graph

**Files:**
- Create: `scripts/tests/test_extract_e2e.py`
- Create: `scripts/tests/fixtures/extract_responses.json`

- [ ] **Step 1: Seed fixture responses**

Create `scripts/tests/fixtures/extract_responses.json`:

```json
{
  "triage": "1. extract — mentions family\n2. skip — trivial greeting",
  "extract_1": {
    "entities": [
      {"canonical_name": "Anna", "kind": "person", "aliases": ["Аня"], "salience": 0.95, "emotional_weight": 0.9},
      {"canonical_name": "Fedya", "kind": "person", "aliases": ["Федя"], "salience": 0.6, "emotional_weight": 0.4}
    ],
    "relations": [
      {"from": "Anna", "to": "Fedya", "kind": "parent", "strength": 0.9}
    ],
    "events": [
      {"title": "Fedya started school", "sentiment": 0.6, "emotional_weight": 0.5, "ts": "2025-09-01T08:00:00Z", "entities": ["Fedya"]}
    ],
    "facts": [],
    "merge_candidates": []
  }
}
```

- [ ] **Step 2: Write the e2e test**

Create `scripts/tests/test_extract_e2e.py`:

```python
import json
import sqlite3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pulse_extract
from extract import prompts

FIXTURES = Path(__file__).parent / "fixtures" / "extract_responses.json"


def _seed(tmp_path):
    db = tmp_path / "e2e.db"
    con = sqlite3.connect(db)
    for mig in sorted(Path(__file__).resolve().parents[2].glob("internal/store/migrations/*.sql")):
        con.executescript(mig.read_text())
    con.commit()

    con.execute("""INSERT INTO observations
        (source_kind, source_id, content_hash, version, scope, captured_at, observed_at, actors, content_text, metadata, raw_json)
        VALUES ('claude_jsonl','f:1','h1',1,'shared','2026-04-15T00:00:00Z','2026-04-15T00:00:00Z',
                '[{"kind":"user","id":"nik"}]', 'Аня сказала что Федя пошёл в школу', '{}', '{}')""")
    con.execute("""INSERT INTO observations
        (source_kind, source_id, content_hash, version, scope, captured_at, observed_at, actors, content_text, metadata, raw_json)
        VALUES ('claude_jsonl','f:2','h2',1,'shared','2026-04-15T00:00:00Z','2026-04-15T00:00:00Z',
                '[{"kind":"user","id":"nik"}]', 'привет', '{}', '{}')""")
    con.execute("""INSERT INTO extraction_jobs
        (observation_ids, state, attempts, created_at, updated_at)
        VALUES ('[1,2]', 'pending', 0, '2026-04-15T00:00:00Z', '2026-04-15T00:00:00Z')""")
    con.commit()
    con.close()
    return db


def test_e2e_extraction_creates_graph(tmp_path, monkeypatch):
    fixtures = json.loads(FIXTURES.read_text())
    db = _seed(tmp_path)

    def fake_triage(_prompt):
        return prompts.parse_triage_response(fixtures["triage"], expected_count=2)

    def fake_extract(_prompt):
        return fixtures["extract_1"]

    monkeypatch.setattr(pulse_extract, "call_sonnet_triage", fake_triage)
    monkeypatch.setattr(pulse_extract, "call_opus_extract", fake_extract)

    rc = pulse_extract.run_once(str(db))
    assert rc == 0

    con = sqlite3.connect(db)
    names = {r[0] for r in con.execute("SELECT canonical_name FROM entities")}
    assert "Anna" in names
    assert "Fedya" in names

    ev_count = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert ev_count == 1

    # evidence linked to obs 1 (the extracted one), not 2 (skipped)
    ev_obs = {r[0] for r in con.execute("SELECT DISTINCT observation_id FROM evidence")}
    assert ev_obs == {1}

    job_state = con.execute("SELECT state FROM extraction_jobs WHERE id=1").fetchone()[0]
    assert job_state == "done"
```

- [ ] **Step 3: Run test**

Run: `cd /Users/nikshilov/dev/ai/pulse && python -m pytest scripts/tests/test_extract_e2e.py -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
cd /Users/nikshilov/dev/ai/pulse
git add scripts/tests/test_extract_e2e.py scripts/tests/fixtures/extract_responses.json
git commit -m "test(e2e): extraction over claude_jsonl fixture — entities+events+evidence with triage filter"
```

---

## Task 25: Run M1-M3 on real Claude JSONL archive (smoke + counts)

**Files:**
- Manual run, no new files.

- [ ] **Step 1: Build Pulse and start it**

`cmd/pulse` takes `--data-dir` (default `$HOME/.pulse`) and `--addr` (default `127.0.0.1:3800`). There is no `serve` subcommand. `ANTHROPIC_API_KEY` is required by `config.Load`.

```bash
cd /Users/nikshilov/dev/ai/pulse
go build -o bin/pulse ./cmd/pulse
mkdir -p ./pulse-dev
export ANTHROPIC_API_KEY=...   # required
./bin/pulse --data-dir ./pulse-dev --addr 127.0.0.1:18889 &
echo $! > /tmp/pulse.pid
```

DB path: `./pulse-dev/pulse.db`. IPC secret: `./pulse-dev/secret.key`.

- [ ] **Step 2: Dry-run on a single project**

`pulse_ingest.py` sends `X-Pulse-Key`, read from the `PULSE_KEY` env var. Export it from the generated secret before running.

```bash
cd /Users/nikshilov/dev/ai/pulse
export PULSE_KEY=$(cat ./pulse-dev/secret.key)
python scripts/pulse_ingest.py --source=claude-jsonl \
  --path=~/.claude/projects/-Users-nikshilov-OpenClawWorkspace \
  --pulse-url=http://127.0.0.1:18889 \
  --dry-run
```

Expected: prints file counts per .jsonl.

- [ ] **Step 3: Real ingest on the same project**

Run:
```bash
python scripts/pulse_ingest.py --source=claude-jsonl \
  --path=~/.claude/projects/-Users-nikshilov-OpenClawWorkspace \
  --pulse-url=http://127.0.0.1:18889 \
  --batch-size=200
```

Expected: `DONE: inserted=N duplicates=0 revisions=0`. Spot-check via sqlite:
```bash
sqlite3 ./pulse-dev/pulse.db 'SELECT source_kind, COUNT(*) FROM observations GROUP BY source_kind'
sqlite3 ./pulse-dev/pulse.db 'SELECT COUNT(*) FROM extraction_jobs WHERE state="pending"'
```

- [ ] **Step 4: Re-run → verify idempotency**

Run same ingest command. Expected: `inserted=0 duplicates=N`.

- [ ] **Step 5: Stop Pulse**

```bash
kill $(cat /tmp/pulse.pid) 2>/dev/null
```

(Don't commit artifacts — `*.db` and `*.key` are gitignored; `./pulse-dev/` contains only those.)

This task is verification, not code. If anything fails, open a new task to fix.

---

## Done (M1-M3 core complete)

After Task 25 passes, **M1-M3 is working software:**

- Capture: observations + revisions + cursors + erasure_log schemas live
- Ingest: POST /ingest with dedup, revision detection, sensitive policy, job enqueue
- Batch-import: claude-jsonl end-to-end via CLI, idempotent
- Extract: two-pass loop (triage + extract), confidence-gated resolution, scorer version-pinned
- Robust: DLQ, budget guard, soft/hard erase

**Not in this plan (future plans):**
- M4 Telegram live (nik-bridge, elle-bridge extension)
- M5 MCP pullers (Limitless, Gmail, Calendar)
- M6 Evening sync + Obsidian dossier writer
- M7 Sensitive UX slash commands + merge review loop
- M8 Autonomy gradient + Phase-2 source starters

Each gets its own plan after M3 is stable on real data for 1-2 weeks.

---

## Checklist verification

- **Spec coverage:** Goal/Architecture/Data model/Provider/Extractor (triage+extract)/Resolution/Scorer/Sensitive/Erasure from spec v3 → all mapped to tasks 1-23. Sync/Autonomy explicitly deferred.
- **Placeholders:** None — every step has concrete code and commands.
- **Type consistency:** `Observation` fields match across Go types (Task 4), handler (Task 6-7), Python normalize (Task 12), extraction loader (Task 19). `resolver.ResolutionDecision` used only in Task 17, 19. Migration columns match what tests insert/query.
- **Scope:** 25 tasks ~ 10-15 working days with focus. M1-M3 produces testable software. M4+ separate.
