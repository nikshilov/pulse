-- 008_consolidation.sql
-- Key-value store for consolidation pipeline state (skip-guard timestamps, etc.)

CREATE TABLE IF NOT EXISTS consolidation_metadata (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
