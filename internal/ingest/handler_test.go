package ingest

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"
	"time"

	"github.com/nkkmnk/pulse/internal/capture"
	"github.com/nkkmnk/pulse/internal/store"
)

func timeParse(t *testing.T, s string) time.Time {
	t.Helper()
	tt, err := time.Parse(time.RFC3339, s)
	if err != nil {
		t.Fatalf("time parse %q: %v", s, err)
	}
	return tt
}

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
		Scope:       "nik",
		CapturedAt:  timeParse(t, "2026-04-15T00:00:00Z"),
		ObservedAt:  timeParse(t, "2026-04-15T00:00:01Z"),
		Actors:      []capture.ActorRef{{Kind: "tg_user", ID: "123"}},
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
		Scope:       "nik",
		CapturedAt:  timeParse(t, "2026-04-15T00:00:00Z"),
		ObservedAt:  timeParse(t, "2026-04-15T00:00:01Z"),
		Actors:      []capture.ActorRef{{Kind: "tg_user", ID: "123"}},
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
