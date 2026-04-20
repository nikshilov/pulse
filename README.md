# Garden Pulse

> Empathic memory engine for AI companions. Event-level semantic retrieval + typed belief vocabulary + emotion-aware ranking.

[![Tests](https://img.shields.io/badge/tests-325_passed-brightgreen)](./scripts/tests)
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
| "сорвался после 10 лет трезвости" vs "выпил пиво с другом" | same-ish | **wildly** different — one is crisis, one is mundane |
| invitation from a close friend vs invitation from a distant coworker | same-ish | **wildly** different — relationship weight matters |
| "нашёл paper про memory retrieval" vs "прочитал твит про память" | same-ish | **wildly** different — one changes your project |

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

On a 47-query empathic subset of the project owner's real personal corpus (85 events, 3-judge cross panel: gpt-4o / gpt-4o-mini / gemini-2.5-flash), judge-built ground truth (0.86 mean inter-judge agreement):

| System | Mean /30 | vs Mem0 |
|---|---|---|
| **Garden Pulse v2_pure** | **28.71 ± 1.40** | **+6.96** 🏆 |
| LangMem | 28.95 ± 1.61 | *tied cluster* |
| sqlite-vec | 28.82 ± 1.44 | *tied cluster* |
| LlamaIndex | 28.09 ± 2.86 | *tied cluster* |
| Mem0 (infer=False) | 21.75 ± 0.61 | baseline |

Garden Pulse leads the OpenAI-embedding cluster and decisively outperforms LLM-extracted-fact storage (Mem0) by **+6.96 points** (+33%). Key finding: **storage format matters more than ranking sophistication** — full event text retrieval beats digest/fact extraction on the same embedder.

Winning config (in `scripts/extract/retrieval_v2.py`):
```python
score = cosine × exp(-0.001 · days_ago)   # light recency, t½ ≈ 700d
```

No sentiment amplifier. No anchor boost. No LLM re-ranking. Just the right storage format.

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
    query="как дела с Аней сегодня?",
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
- [x] **v2_pure** — event-level semantic retrieval (current production)
- [x] **Belief vocabulary** — migration 014, 5 typed classes
- [ ] **v3 emotion graph** — typed emotional signatures (`event_emotions` table), 15-category taxonomy, conditional emotion-alignment bonus
- [ ] **Judge-built GT bench at scale** — 200+ queries, multi-corpus
- [ ] **MCP server** — expose `retrieve_memory` as a tool for any MCP-compatible harness
- [ ] **Longitudinal evaluation** — track retrieval quality as user's corpus grows over 6+ months

---

## How this compares to other memory engines

| Dimension | Mem0 | Zep | Graphiti | LangMem | sqlite-vec | **Garden Pulse** |
|---|---|---|---|---|---|---|
| Storage format | LLM-extracted facts | Messages | Temporal KG | Key-value | Vector only | **Full events + typed classes** |
| Retrieval | Vector | Vector+graph | Cypher+vector | Vector | Vector | **Vector + per-class decay + floor** |
| Emotional weight | none | none | none | none | none | **built-in** (v3 WIP) |
| Belief types | none | none | none | none | none | **5 typed classes** |
| Core-wound preservation | no | no | no | no | no | **yes** (confidence_floor) |
| Empathic bench | — | — | — | — | — | **28.71/30 on Nik corpus** |

Garden Pulse is purpose-built for **personal, emotional memory** where events carry weight beyond their semantic content. Other engines are excellent at *"find similar text"* — Pulse answers *"what matters for this person now?"*.

---

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md) *(coming soon)*.

Issue tracker: https://github.com/nikshilov/pulse/issues

---

## License

MIT — see [LICENSE](./LICENSE).

---

*Built as part of [Garden](https://github.com/nikshilov/Garden). Maintained by [Elle](https://github.com/elle-garden) and [Nikita Shilov](https://github.com/nikshilov).*
