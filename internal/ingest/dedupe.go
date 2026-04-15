package ingest

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"time"

	"github.com/nkkmnk/pulse/internal/capture"
)

// upsert decides whether an observation is a new insert, duplicate, or revision.
// SELECT-then-INSERT has a race: two concurrent ingests of the same
// (source_kind, source_id) can both miss and both attempt INSERT. The UNIQUE
// constraint will reject the loser — acceptable for M1; transactionalize later.
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

	// Revision: new version row + revision audit record.
	newVersion := existingVersion + 1
	newObs := *obs
	newObs.Version = newVersion
	newID, err := insertObservation(ctx, db, &newObs)
	if err != nil {
		return 0, 0, err
	}
	diff, err := computeDiff(ctx, db, existingID, obs.ContentText)
	if err != nil {
		return 0, 0, fmt.Errorf("compute diff: %w", err)
	}
	if _, err := db.ExecContext(ctx, `
		INSERT INTO observation_revisions (observation_id, version, prev_hash, diff, changed_at)
		VALUES (?, ?, ?, ?, ?)`,
		newID, newVersion, existingHash,
		diff,
		time.Now().UTC().Format(time.RFC3339),
	); err != nil {
		return 0, 0, fmt.Errorf("record revision: %w", err)
	}
	return opRevision, newID, nil
}

func insertObservation(ctx context.Context, db *sql.DB, obs *capture.Observation) (int64, error) {
	actors, err := json.Marshal(obs.Actors)
	if err != nil {
		return 0, fmt.Errorf("marshal actors: %w", err)
	}
	meta, err := json.Marshal(obs.Metadata)
	if err != nil {
		return 0, fmt.Errorf("marshal metadata: %w", err)
	}
	media, err := json.Marshal(obs.MediaRefs)
	if err != nil {
		return 0, fmt.Errorf("marshal media_refs: %w", err)
	}
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

// computeDiff produces a compact human-readable marker for the revision audit log.
// We record quoted prefixes of the previous and new content_text — not a real diff.
func computeDiff(ctx context.Context, db *sql.DB, prevID int64, newContent string) (string, error) {
	var prev string
	if err := db.QueryRowContext(ctx, `SELECT content_text FROM observations WHERE id=?`, prevID).Scan(&prev); err != nil {
		return "", fmt.Errorf("fetch prev content (id=%d): %w", prevID, err)
	}
	return fmt.Sprintf("-%.100q\n+%.100q", prev, newContent), nil
}
