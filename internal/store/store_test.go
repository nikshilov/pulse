package store

import (
	"path/filepath"
	"testing"
)

func TestOpenCreatesSchema(t *testing.T) {
	dbPath := filepath.Join(t.TempDir(), "test.db")
	db, err := Open(dbPath)
	if err != nil {
		t.Fatalf("Open failed: %v", err)
	}
	defer db.Close()

	var journalMode string
	if err := db.QueryRow("PRAGMA journal_mode").Scan(&journalMode); err != nil {
		t.Fatalf("journal_mode query: %v", err)
	}
	if journalMode != "wal" {
		t.Errorf("expected WAL mode, got %q", journalMode)
	}

	var count int
	if err := db.QueryRow("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='outbox'").Scan(&count); err != nil {
		t.Fatalf("outbox table check: %v", err)
	}
	if count != 1 {
		t.Errorf("expected outbox table, got count %d", count)
	}

	var version int
	if err := db.QueryRow("SELECT MAX(version) FROM schema_meta").Scan(&version); err != nil {
		t.Fatalf("schema_meta: %v", err)
	}
	if version != 5 {
		t.Errorf("expected schema version 5, got %d", version)
	}
}

func TestOpenIsIdempotent(t *testing.T) {
	dbPath := filepath.Join(t.TempDir(), "test.db")
	db1, err := Open(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	db1.Close()
	db2, err := Open(dbPath)
	if err != nil {
		t.Fatalf("second open failed: %v", err)
	}
	defer db2.Close()
	var version int
	if err := db2.QueryRow("SELECT MAX(version) FROM schema_meta").Scan(&version); err != nil {
		t.Fatal(err)
	}
	if version != 5 {
		t.Errorf("expected still at version 5, got %d", version)
	}
}

func TestContextSchemaApplied(t *testing.T) {
	dbPath := filepath.Join(t.TempDir(), "test.db")
	db, err := Open(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	// All five context tables exist.
	for _, table := range []string{"sessions", "messages", "memory_snapshots", "compaction_events", "pending_promotions"} {
		var n int
		if err := db.QueryRow(
			"SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?",
			table,
		).Scan(&n); err != nil {
			t.Fatalf("check %s: %v", table, err)
		}
		if n != 1 {
			t.Errorf("expected table %q to exist", table)
		}
	}

	// FTS5 virtual table exists.
	var fts int
	if err := db.QueryRow(
		"SELECT count(*) FROM sqlite_master WHERE type='table' AND name='sessions_fts'",
	).Scan(&fts); err != nil {
		t.Fatal(err)
	}
	if fts != 1 {
		t.Error("expected sessions_fts virtual table")
	}

	// Migration version bumped to 5 (005_graph added).
	var version int
	if err := db.QueryRow("SELECT MAX(version) FROM schema_meta").Scan(&version); err != nil {
		t.Fatal(err)
	}
	if version != 5 {
		t.Errorf("expected schema version 5, got %d", version)
	}
}

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
	if err != nil {
		t.Fatal(err)
	}
	_, err = db.Exec(`INSERT INTO entity_identities (entity_id, source_kind, identifier, first_seen) VALUES (1,'tg','123','2026-04-15T00:00:00Z')`)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`DELETE FROM entities WHERE id=1`); err != nil {
		t.Fatal(err)
	}

	var count int
	db.QueryRow(`SELECT COUNT(*) FROM entity_identities WHERE entity_id=1`).Scan(&count)
	if count != 0 {
		t.Errorf("expected cascade delete, got %d identities", count)
	}

	// CASCADE on entity_merge_proposals
	var eaID, ebID int64
	if err = db.QueryRow(`INSERT INTO entities (canonical_name, kind, first_seen, last_seen) VALUES ('a','person','2026-04-15T00:00:00Z','2026-04-15T00:00:00Z') RETURNING id`).Scan(&eaID); err != nil {
		t.Fatal(err)
	}
	if err = db.QueryRow(`INSERT INTO entities (canonical_name, kind, first_seen, last_seen) VALUES ('b','person','2026-04-15T00:00:00Z','2026-04-15T00:00:00Z') RETURNING id`).Scan(&ebID); err != nil {
		t.Fatal(err)
	}
	_, err = db.Exec(`INSERT INTO entity_merge_proposals (from_entity_id, to_entity_id, confidence, evidence_md, state, proposed_at) VALUES (?,?,0.85,'test','pending','2026-04-15T00:00:00Z')`, eaID, ebID)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`DELETE FROM entities WHERE id=?`, eaID); err != nil {
		t.Fatalf("entity with merge proposal should cascade-delete: %v", err)
	}
	db.QueryRow(`SELECT COUNT(*) FROM entity_merge_proposals WHERE from_entity_id=?`, eaID).Scan(&count)
	if count != 0 {
		t.Errorf("expected cascade on entity_merge_proposals, got %d", count)
	}

	// CASCADE on sensitive_actors
	_, err = db.Exec(`INSERT INTO sensitive_actors (entity_id, policy, added_at, added_by) VALUES (?,'redact_content','2026-04-15T00:00:00Z','nik')`, ebID)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`DELETE FROM entities WHERE id=?`, ebID); err != nil {
		t.Fatalf("entity with sensitive policy should cascade-delete: %v", err)
	}
	db.QueryRow(`SELECT COUNT(*) FROM sensitive_actors WHERE entity_id=?`, ebID).Scan(&count)
	if count != 0 {
		t.Errorf("expected cascade on sensitive_actors, got %d", count)
	}
}

func TestSessionsFTSTriggersWork(t *testing.T) {
	dbPath := filepath.Join(t.TempDir(), "test.db")
	db, err := Open(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	// Insert a session — FTS5 insert trigger should fire.
	_, err = db.Exec(`
        INSERT INTO sessions (name, summary_markdown, memory_snapshot_hash, created_at)
        VALUES ('Anna closeness', 'Nik felt distant from Anna tonight.', 'abc123', '2026-04-14T10:00:00Z')
    `)
	if err != nil {
		t.Fatalf("insert session: %v", err)
	}

	// FTS5 MATCH finds it by summary keyword.
	var foundName string
	err = db.QueryRow(`
        SELECT s.name FROM sessions_fts
        JOIN sessions s ON s.id = sessions_fts.rowid
        WHERE sessions_fts MATCH 'Anna'
    `).Scan(&foundName)
	if err != nil {
		t.Fatalf("fts search: %v", err)
	}
	if foundName != "Anna closeness" {
		t.Errorf("unexpected match: %q", foundName)
	}
}
