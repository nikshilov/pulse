# Changelog

All notable changes to Garden Pulse are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) principles.

## [v2.0.0] — 2026-04-18

**Pulse beats Mem0 by +6.96 points on a 3-judge empathic-memory panel. Release includes belief vocabulary, event-level semantic retrieval as the new default, and a reproducible 47-query benchmark.**

### Added

- **Event-level semantic retrieval** (`scripts/extract/retrieval_v2.py`)
  - `retrieve_events()` returns top-k events by `cosine × recency` on query vs event text
  - `embed_events()` backfill for existing events, idempotent via model filter
  - Pure-Python cosine, no numpy dep
  - Supports fake-local (tests) and `openai-text-embedding-3-large` (production)
  - Graceful fallback on pre-migration-013 databases
- **Migration 013** — `event_embeddings` table (3072-dim JSON vectors, model-filtered)
- **Migration 014** — typed belief vocabulary on events + facts
  - 5 classes: `axiom / self_model / user_model / operational / hypothesis`
  - `confidence_floor ∈ [0, 1]` preserves salience against decay
  - `archivable` flag pins events against consolidation
  - `provenance` field for audit trail (`memory_pattern / interactive_memory / idle_background / sleep_reflection / manual`)
  - Per-class decay rates at retrieval time: axiom=0.0 → hypothesis=0.005
- **18 new tests** covering retrieval (12) + belief vocabulary (6) — full suite: **325 passed, 7 skipped**
- **Comprehensive README** with architecture diagram, benchmark table, quickstart, roadmap
- **MIT LICENSE**

### Changed

- Default retrieval config: `α=0, λ=0.001, anchor=1.0` (winning sweep config, +6.96 vs Mem0)
- `retrieve_events()` uses per-belief-class decay by default (`use_belief_class=True`); pass `False` for pre-v2 uniform-λ behavior
- Documentation rebranded as **Garden Pulse** throughout

### Benchmark results (47-query empathic subset, 3-judge panel)

| System | Mean /30 | Δ vs Mem0 |
|---|---|---|
| **Garden Pulse v2_pure** | **28.71 ± 1.40** | **+6.96** |
| LangMem | 28.95 ± 1.61 | tied |
| sqlite-vec | 28.82 ± 1.44 | tied |
| LlamaIndex | 28.09 ± 2.86 | tied |
| Mem0 (infer=False) | 21.75 ± 0.61 | baseline |

Judge-built GT with 0.86 mean inter-judge agreement across 4 retrieval pools. Full methodology + artifacts: [bench/baselines/EMPATHIC_SUBSET_RESULTS.md](./scripts/bench/baselines/EMPATHIC_SUBSET_RESULTS.md).

### Key findings

1. **Storage format > ranking sophistication.** Full original event text retrieval beats LLM-extracted-fact storage by +7 points on the same judge panel with the same embedder.
2. **Any sentiment amplifier hurts on this corpus.** `α > 0` drops score by 9+ points. Full-text embeddings already encode emotional context — adding a multiplier distorts good rankings.
3. **v2_pure sits at the OpenAI-embedding ceiling.** LangMem, sqlite-vec, and LlamaIndex tie within σ. Differentiation from Mem0 comes from storage format, not ranking math.
4. **Embedding-derived GT can structurally disadvantage emotion-aware re-rankers.** Judge-built GT (Borda 3/2/1 from 3-judge panels) is the correct evaluation methodology for empathic retrieval.

### Known limitations

- Typed emotion signatures (`event_emotions` table, 15-category taxonomy) are **designed but not yet shipped** — blocked on larger corpus + hand-labeled GT per v3 simulation ceiling analysis.
- LlamaIndex adapter currently loses `event_id` mapping for some doc_ids — judge scores valid, Recall@3 metric unreliable for that system.
- Bench corpus is a single owner's personal memory — generalization claims require multi-user evaluation (roadmap).

## [v1.x] — prior releases

- Entity-level keyword-BFS retrieval (`scripts/extract/retrieval.py`)
- Ingestion pipeline: Sonnet triage → Opus extract → graph populate
- Migrations 003-012: observations, extraction jobs, graph core, Phase 1 enrichment, consolidation, safety, self-entity, embeddings, graph snapshots
- Intent classifier (rules + LLM backends)
- Cross-judge benchmark harness
- Telegram bridge + outbox worker (production VDS deployment)

v1.x kept in `scripts/extract/retrieval.py` for "who is this person/thing" entity queries. v2_pure is the new default for "what moment matters now" empathic retrieval.
