-- 013_event_embeddings.sql
-- Event-level dense vector embeddings — the retrieval primary index.
--
-- Rationale (2026-04-18 bench):
--   - Pulse v1 entity-level keyword-BFS retrieval fell through to static salience
--     fallback on 98% of Russian conversational bursts (46/47 same top-3 wound anchors).
--   - Pulse v2_pure (event-level cosine + light recency, α=0) scored 28.71 ± 1.40
--     on Nik empathic corpus 47-query subset vs Mem0's 21.75 (+6.96). See
--     `scripts/bench/baselines/EMPATHIC_SUBSET_RESULTS.md`.
--   - Winning path = embed events directly, cosine retrieve, light recency decay
--     (λ=0.001, half-life ~700d), no sentiment amplifier, no anchor boost.
--
-- vector stored as JSON array of floats — same pattern as entity_embeddings (011).
-- At small scale (<100k events) naive cosine in Python is fast enough.
CREATE TABLE IF NOT EXISTS event_embeddings (
    event_id     INTEGER PRIMARY KEY,
    model        TEXT NOT NULL,
    dim          INTEGER NOT NULL,
    vector_json  TEXT NOT NULL,
    text_source  TEXT NOT NULL,       -- the exact text that was embedded (title+description)
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_event_embeddings_model ON event_embeddings(model);
