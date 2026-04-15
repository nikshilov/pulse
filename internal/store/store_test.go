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
	if version != 2 {
		t.Errorf("expected schema version 2, got %d", version)
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
	if version != 2 {
		t.Errorf("expected still at version 2, got %d", version)
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

	// Migration version bumped to 2.
	var version int
	if err := db.QueryRow("SELECT MAX(version) FROM schema_meta").Scan(&version); err != nil {
		t.Fatal(err)
	}
	if version != 2 {
		t.Errorf("expected schema version 2, got %d", version)
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
