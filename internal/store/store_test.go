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
	if version != 3 {
		t.Errorf("expected schema version 3, got %d", version)
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
	if version != 3 {
		t.Errorf("expected still at version 3, got %d", version)
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

	// Migration version bumped to 3 (003_observations added).
	var version int
	if err := db.QueryRow("SELECT MAX(version) FROM schema_meta").Scan(&version); err != nil {
		t.Fatal(err)
	}
	if version != 3 {
		t.Errorf("expected schema version 3, got %d", version)
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
