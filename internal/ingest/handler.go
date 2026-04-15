package ingest

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"github.com/nkkmnk/pulse/internal/capture"
	"github.com/nkkmnk/pulse/internal/store"
)

// Handler processes POST /ingest requests.
type Handler struct {
	store *store.Store
}

// NewHandler returns a new Handler backed by the given store.
func NewHandler(s *store.Store) *Handler {
	return &Handler{store: s}
}

type request struct {
	Observations []capture.Observation `json:"observations"`
}

type response struct {
	Inserted   int     `json:"inserted"`
	Duplicates int     `json:"duplicates"`
	Revisions  int     `json:"revisions"`
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
	if obs.SourceKind == "" {
		return fmt.Errorf("source_kind required")
	}
	if obs.SourceID == "" {
		return fmt.Errorf("source_id required")
	}
	if obs.Scope != "elle" && obs.Scope != "nik" && obs.Scope != "shared" {
		return fmt.Errorf("scope must be elle|nik|shared, got %q", obs.Scope)
	}
	if obs.CapturedAt.IsZero() {
		return fmt.Errorf("captured_at required")
	}
	if obs.ObservedAt.IsZero() {
		obs.ObservedAt = time.Now().UTC()
	}
	if obs.Version == 0 {
		obs.Version = 1
	}
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
	if err != nil {
		return 0, err
	}
	return res.LastInsertId()
}
