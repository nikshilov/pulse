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
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { s.Close() })
	return s
}

func TestSoftEraseEntity(t *testing.T) {
	s := openTestStore(t)
	db := s.DB()

	// Fixture: one entity with two observations linked via evidence
	_, err := db.Exec(`INSERT INTO entities (canonical_name, kind, first_seen, last_seen) VALUES ('Alice','person','2026-04-15T00:00:00Z','2026-04-15T00:00:00Z')`)
	if err != nil {
		t.Fatal(err)
	}
	entID := int64(1)

	_, err = db.Exec(`INSERT INTO observations (source_kind, source_id, content_hash, version, scope, captured_at, observed_at, actors, content_text) VALUES
        ('tg','m:1','h1',1,'nik','2026-04-15T00:00:00Z','2026-04-15T00:00:00Z','[]','private message 1'),
        ('tg','m:2','h2',1,'nik','2026-04-15T00:00:00Z','2026-04-15T00:00:00Z','[]','private message 2')`)
	if err != nil {
		t.Fatal(err)
	}

	_, err = db.Exec(`INSERT INTO evidence (subject_kind, subject_id, observation_id, created_at) VALUES
        ('entity',1,1,'2026-04-15T00:00:00Z'),
        ('entity',1,2,'2026-04-15T00:00:00Z')`)
	if err != nil {
		t.Fatal(err)
	}

	// Act
	e := NewEraser(s)
	err = e.SoftErase(context.Background(), entID, "nik", "user request")
	if err != nil {
		t.Fatalf("soft erase: %v", err)
	}

	// Assert: content_text is NULL, redacted=1 for both observations
	var redactedCount int
	if err := db.QueryRow(`SELECT COUNT(*) FROM observations WHERE redacted=1 AND content_text IS NULL`).Scan(&redactedCount); err != nil {
		t.Fatalf("query redacted count: %v", err)
	}
	if redactedCount != 2 {
		t.Errorf("expected 2 redacted, got %d", redactedCount)
	}

	// Evidence preserved (row still there)
	var evCount int
	if err := db.QueryRow(`SELECT COUNT(*) FROM evidence WHERE subject_id=1`).Scan(&evCount); err != nil {
		t.Fatalf("query evidence count: %v", err)
	}
	if evCount != 2 {
		t.Errorf("evidence should be preserved, got %d", evCount)
	}

	// erasure_log row present
	var logCount int
	if err := db.QueryRow(`SELECT COUNT(*) FROM erasure_log WHERE op_kind='soft'`).Scan(&logCount); err != nil {
		t.Fatalf("query erasure_log count: %v", err)
	}
	if logCount != 1 {
		t.Errorf("expected 1 erasure_log row, got %d", logCount)
	}
}
