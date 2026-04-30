# Garden Pulse

> Empathic memory engine for AI companions. Event-level semantic retrieval + typed belief vocabulary + emotion-aware ranking.

[![Tests](https://img.shields.io/badge/tests-342_passed-brightgreen)](./scripts/tests)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![Go](https://img.shields.io/badge/go-1.22%2B-00ADD8)](https://go.dev/)
[![License](https://img.shields.io/badge/license-MIT-blue)](./LICENSE)
[![Part of Garden](https://img.shields.io/badge/part_of-Garden-8B4513)](https://github.com/nikshilov/Garden)

Part of the [Garden](https://github.com/nikshilov/Garden) project — therapeutic-grade AI companion infrastructure.

---

## The problem

Most memory engines for LLM companions answer the wrong question:

> *"What text is most similar to this query?"*

Garden Pulse answers the harder one:

> **"Given who this person is, what moment from their life should surface right now?"**

Pure cosine retrieval treats these pairs identically:

| Pair | Cosine says | Empathic companion needs |
|---|---|---|
| "relapsed after 10 years sober" vs "had a beer with a friend" | same-ish | **wildly** different — one is crisis, one is mundane |
| invitation from a close friend vs invitation from a distant coworker | same-ish | **wildly** different — relationship weight matters |
| "found a paper on memory retrieval" vs "read a tweet about memory" | same-ish | **wildly** different — one changes your project |

Garden Pulse disambiguates via **typed belief weights**, **emotional signatures**, and **recency per belief class**.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Ingestion: conversations → typed observations         │
│  triage (Sonnet) → extract (Opus) → graph apply        │
├─────────────────────────────────────────────────────────┤
│  Storage: SQLite + event-level embeddings              │
│  • entities / relations / facts                        │
│  • events with typed belief_class + confidence_floor   │
│  • event_embeddings (text-embedding-3-large, 3072d)    │
├─────────────────────────────────────────────────────────┤
│  Retrieval:                                            │
│                                                         │
│  score = cosine(query, event)                          │
│        × exp(-λ[belief_class] · days_ago)              │
│        (floored at cosine × confidence_floor)          │
│                                                         │
│  5 belief classes (axiom → hypothesis)                 │
│  confidence_floor preserves core wounds                │
│  archivable flag pins defining moments                 │
└─────────────────────────────────────────────────────────┘
```

---

## Benchmark

### Empathic Memory Bench v3 (2026-04-24) — Pulse v3 SOTA

8 LLM judges × 8 model checkpoints (6 vendor families) × 35 tests × 5 axes, on [bench v3 corpus](https://github.com/nikshilov/bench):

| System | overall | core | stateful | chain | multi_signal |
|---|---|---|---|---|---|
| cosine | 4.83 | 6.05 | 0.17 | 1.75 | 5.34 |
| bm25 | 3.07 | 4.24 | 0.01 | 1.83 | 1.59 |
| hybrid | 4.43 | 5.65 | 0.29 | 2.58 | 3.64 |
| **Pulse v3** | **6.38** | **6.90** | **6.44** | **4.50** | **6.26** |

Delta vs best baseline per axis: overall **+1.55 (+32% vs cosine)**, **stateful +6.15 (×22 vs hybrid / ×38 vs cosine)**, chain +1.92 (+74% vs hybrid), core +0.85 (no regression vs cosine).
Opus wins 26/35 tests (74%). Krippendorff α on stateful axis = **0.81 (strong)** — cross-judge consensus.

Judges: Moonshot Kimi K2.6 + K2-0711-preview, Z.ai GLM-5 + GLM-5.1, Alibaba Qwen3-Max, DeepSeek V3.2, OpenAI GPT-5.4, Anthropic Claude Opus 4.7 — 8 model checkpoints across 6 vendor families.

### External validation (three independent benchmarks)

| benchmark | score | notes |
|---|---|---|
| **LongMemEval_S** (ICLR 2025, 500 Qs) | **68.89%** | overall, -3.2 pts vs oracle |
| **ES-MemEval** (Feb 2026, 1427 Qs) | **76%** (1.519/2.0 LLM-judge) | comparable to gpt-4o+RAG |
| **LoCoMo** (ACL 2024, 1986 Qs × 10 convs) | **32.51% F1**, 62.78% adv refusal | cosine + Cohere embed |

These three are run with **Pulse v2_pure** — the cosine-plus-recency baseline Pulse v3 collapses to when no state / emotion / anchor signals are provided (none of the external datasets carry those fields). v3 == v2_pure on this data by construction, so the external numbers validate Pulse's *foundation*, not v3's conditional boosts specifically (those are validated only on bench v3's stateful / chain / multi-signal axes, which is what empathic-memory bench v3 exists for).

See [github.com/nikshilov/bench](https://github.com/nikshilov/bench) for reproduction scripts and raw JSON.

### v2_pure baseline (2026-04-18) — unchanged

On a 47-query empathic subset of the project owner's real personal corpus (85 events, 3-judge cross panel: gpt-4o / gpt-4o-mini / gemini-2.5-flash):

| System | Mean /30 |
|---|---|
| **Pulse v2_pure** | **28.71 ± 1.40** 🏆 |
| LangMem | 28.95 ± 1.61 |
| sqlite-vec | 28.82 ± 1.44 |
| LlamaIndex | 28.09 ± 2.86 |
| Mem0 (infer=False) | 21.75 ± 0.61 |

Pulse v2_pure still wins the OpenAI-embedding cluster by **+6.96 pts** (+33%) over Mem0 on that bench. Key finding at the time: **storage format matters more than ranking sophistication** — full event text retrieval beats digest/fact extraction on the same embedder. v3 extends v2 with conditional boosts that stack only when their signals genuinely exist.

---

## Pulse v3 (2026-04-24) — conditional multi-signal ranking

v3 wraps v2_pure with five **conditional** boosts — each term activates only when its input signal exists, so queries without state / emotion / anchor information produce bit-identical results to v2_pure (no regression on plain retrieval).

```
score = cosine
      × exp(-λ[belief_class, user_flag] · days_ago)   # anchor-aware decay
      × (1 + β · emotion_alignment)    if query_emotion ≥ 0.5   # conditional emotion boost
      × (1 + γ · state_fit)            if body stressed/restored # conditional state boost
      × (1 + δ_anchor · user_flag)     if rank ≤ 8              # anchor-priority
      × (1 + δ_date  · date_proximity) if snapshot_days_ago set  # date-proximity
```

Key ideas:

- **Anchor-aware decay (λ_anchor = 0.001)** — events with `user_flag=true` (structural-truth anchors like marriage, grief, identity events) decay twice as slowly as regular events. Half-life 693d vs 347d. Matches the v2 `user_model` tier exactly.
- **Conditional gating** — Phase D (2026-04-20) proved that an always-on emotion cosine term monotonically **hurts** retrieval (β=0 → β=3 drops NDCG from 43.77 to 27.72). v3 activates emotion boost **only** when the query has a dominant emotion (max ≥ 0.5 after query-emotion inference). Same discipline applies to state boost (gated by body-stressed or body-restored signals) and anchor boost (gated by `user_flag=true`).
- **Emotion-hint query augmentation (Phase 5.5)** — when `user_state.mood_vector` has a dominant emotion, a short hint string is appended to the query *before* embedding (e.g. "conflict navigation repair" for anger, "wound self-blame rejection" for shame). This is what lifts the stateful axis from 3.60 to 6.60 single-handedly.
- **Date-proximity boost (Phase 5.2)** — when `user_state.snapshot_days_ago` is provided (e.g. from a real Apple Health snapshot), events whose `days_ago` is close get a small boost via a stepped curve (same day = 1.0, within a week = 0.7, etc.).
- **Chain expansion** — if `return_chain=True`, top-K events are expanded via `event_chains` table (BFS depth 3) and returned as an ordered sequence rather than a set.

Schema additions (migration 015):
- `event_emotions` — Plutchik-10 floats per event (`joy, sadness, anger, fear, trust, disgust, anticipation, surprise, shame, guilt`)
- `event_chains` — `parent_id → child_id` with `strength` and `kind` (for causal/temporal links)
- `query_emotion_cache` — inference cache for the emotion classifier

Source files:
- [`scripts/extract/retrieval_v3.py`](./scripts/extract/retrieval_v3.py) — full Python implementation
- [`scripts/extract/emotion_classifier.py`](./scripts/extract/emotion_classifier.py) — Qwen-based Plutchik tagger
- [`internal/store/migrations/015_emotions_chains.sql`](./internal/store/migrations/015_emotions_chains.sql) — schema

Tests: [`scripts/tests/test_retrieval_v3.py`](./scripts/tests/test_retrieval_v3.py) — includes no-regression property (v3 with no state == v2_pure).

---

## Belief vocabulary

Five typed classes with per-class decay rates (migration 014):

| Class | Decay λ | Half-life | Floor default | Use case |
|---|---|---|---|---|
| `axiom` | **0.0** | ∞ | 0.0 | Permanent truths — core wounds, companion identity |
| `self_model` | 0.0005 | ~1400 days | 0.0 | Companion's introspective facts |
| `user_model` | 0.001 | ~700 days | 0.0 | User's psychological profile, long-term preferences |
| `operational` | 0.003 | ~230 days | 0.0 | Day-to-day context (default) |
| `hypothesis` | 0.005 | ~140 days | 0.0 | Provisional reads awaiting confirmation |

**`confidence_floor` ∈ [0, 1]** — minimum post-decay score. A `user_model` belief with `floor=0.85` stays salient against semantic match even at 10+ years old.

**`archivable: 0`** — pins event against consolidation/archival.

**`provenance`** — audit trail: `memory_pattern` / `interactive_memory` / `idle_background` / `sleep_reflection` / `manual`.

---

## Quickstart

### Requirements
- Python 3.11+
- Go 1.22+
- OpenAI API key (for `text-embedding-3-large`)
- Anthropic API key (for extraction pipeline: Sonnet triage + Opus extract)

### Install

```bash
git clone git@github.com:nikshilov/pulse.git
cd pulse

# Go build
go build -o bin/pulse ./cmd/pulse

# Python deps (if using scripts)
pip install openai anthropic
```

### One-line dev workflow (Make)

```bash
make build       # compile bin/pulse
make test        # Go (./...) + Python (scripts/tests/)
make run         # start the server on 127.0.0.1:18789
make demo        # ingest -> retrieve end-to-end (see examples/03-end-to-end)
make help        # list all targets
```

Runnable examples live in [`examples/`](./examples/) — three minimal
Python scripts (stdlib only) demonstrating ingest, retrieval, and a
chained end-to-end demo against a running server.

### Initialize the graph

```bash
# Creates pulse.db with all 14 migrations applied
bin/pulse
```

### Python retrieval API

```python
import sqlite3
from extract.retrieval_v2 import embed_events, retrieve_events

con = sqlite3.connect("pulse.db")

# One-time: backfill embeddings for existing events
embed_events(con, embedder_model="openai-text-embedding-3-large")

# Retrieve top-3 relevant events
events = retrieve_events(
    con,
    query="how are things with my partner today?",
    top_k=3,
    embedder_model="openai-text-embedding-3-large",
)

for e in events:
    print(f"  #{e['id']} ({e['belief_class']}, {e['days_ago']}d ago): {e['text'][:80]}")
    print(f"     score={e['score']:.3f}  cosine={e['cosine']:.3f}  λ={e['effective_lambda']}")
```

### Seed an axiom (event that never decays)

```sql
INSERT INTO events (title, description, sentiment, ts, belief_class, confidence_floor, archivable)
VALUES (
  'core-wound',
  'Never been chosen without proving value first. Mother''s conditional worth.',
  -2.0,
  '2020-01-15T00:00:00Z',
  'axiom',     -- no decay
  0.85,        -- preserved even if retrieval finds 0.2 cosine match
  0            -- never archivable
);
```

---

## Tests

```bash
python3 -m pytest scripts/tests/ -q
# 325 passed, 7 skipped
```

Test coverage includes:
- Retrieval correctness (cosine, recency, top-k bounds, cross-model isolation)
- Belief vocabulary (axiom zero-decay, hypothesis fast-decay, floor preservation, CHECK constraints)
- Graceful fallback on pre-migration-014 databases
- All 14 migrations applied cleanly
- E2E extraction pipeline

---

## Roadmap

- [x] **v1** — entity-level keyword-BFS retrieval (superseded)
- [x] **v2_pure** — event-level semantic retrieval (current production default)
- [x] **Belief vocabulary** — migration 014, 5 typed classes
- [x] **v3 emotion + state graph** — Plutchik-10 tags, chain table, conditional emotion/state/anchor/date boosts, SOTA on bench v3 (overall +32% vs cosine, stateful ×22 vs hybrid)
- [x] **External validation** — LongMemEval_S 68.89%, ES-MemEval 76%, LOCOMO 32.51%
- [ ] **Judge-built GT bench at scale** — 200+ queries, multi-corpus
- [ ] **MCP server** — expose `retrieve_memory` as a tool for any MCP-compatible harness
- [ ] **Longitudinal evaluation** — track retrieval quality as user's corpus grows over 6+ months

---

## How this compares to other memory engines

| Dimension | Mem0 | Zep | Graphiti | LangMem | sqlite-vec | **Garden Pulse** |
|---|---|---|---|---|---|---|
| Storage format | LLM-extracted facts | Messages | Temporal KG | Key-value | Vector only | **Full events + typed classes** |
| Retrieval | Vector | Vector+graph | Cypher+vector | Vector | Vector | **Vector + per-class decay + floor** |
| Emotional weight | none | none | none | none | none | **built-in** (v3 shipped) |
| Stateful retrieval | no | no | no | no | no | **yes** (mood_vector + body state) |
| Belief types | none | none | none | none | none | **5 typed classes** |
| Core-wound preservation | no | no | no | no | no | **yes** (confidence_floor) |
| Empathic bench | — | — | — | — | — | **SOTA on bench v3** (6.38 overall vs cosine 4.83; stateful ×22 vs hybrid / ×38 vs cosine) |

Garden Pulse is purpose-built for **personal, emotional memory** where events carry weight beyond their semantic content. Other engines are excellent at *"find similar text"* — Pulse answers *"what matters for this person now?"*.

---

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md) for dev setup, test commands,
code style, and PR guidelines. Runnable examples in [`examples/`](./examples/).

Issue tracker: https://github.com/nikshilov/pulse/issues

---

## License

MIT — see [LICENSE](./LICENSE).

---

*Built as part of [Garden](https://github.com/nikshilov/Garden). Maintained by [Elle](https://github.com/elle-garden) and [Nikita Shilov](https://github.com/nikshilov).*
