# Garden Pulse

**Empathic memory engine for AI companions.** Event-level semantic retrieval + typed belief vocabulary + emotion-aware ranking.

Part of the [Garden](https://github.com/nikshilov/Garden) project — therapeutic-grade AI companion infrastructure.

## What it does

Pulse stores and retrieves personal memories for an AI companion in a way that honors emotional weight, not just semantic similarity. The engine answers a harder question than "what text is most like this query":

> **"Given who this person is, what moment from their life should surface right now?"**

Built for the case where a companion sees:
- a user's breakdown after 10 years sobriety vs a casual beer with a friend
- an invitation from a close friend vs a distant coworker
- a technical article found this morning that changes everything for the user's project

Pure cosine retrieval treats these the same. Garden Pulse doesn't.

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Ingestion: conversations → typed observations  │
│  (triage Sonnet → extract Opus → graph apply)   │
├─────────────────────────────────────────────────┤
│  Storage: SQLite + event-level embeddings       │
│  • entities / relations / facts                 │
│  • events with typed belief_class + floor       │
│  • event_embeddings (text-embedding-3-large)    │
├─────────────────────────────────────────────────┤
│  Retrieval: cosine × per-class decay × floor    │
│  • 5 belief classes (axiom → hypothesis)        │
│  • confidence_floor preserves core wounds       │
│  • archivable flag pins defining moments        │
└─────────────────────────────────────────────────┘
```

## Benchmark

On a 47-query empathic subset of the project owner's real corpus (85 events, 3-judge cross panel of gpt-4o / gpt-4o-mini / gemini-2.5-flash):

| System | Mean /30 | vs Mem0 |
|---|---|---|
| **Garden Pulse v2_pure** | **28.71 ± 1.40** | **+6.96** |
| LangMem | 28.95 ± 1.61 | (tied cluster) |
| sqlite-vec | 28.82 ± 1.44 | (tied cluster) |
| LlamaIndex | 28.09 ± 2.86 | (tied cluster) |
| Mem0 (infer=False) | 21.75 ± 0.61 | baseline |

Pulse leads the OpenAI-embedding cluster and decisively outperforms LLM-extracted-fact storage (Mem0) by 7 points. Methodology: judge-built GT with Borda 3/2/1 aggregation, 0.86 mean inter-judge agreement.

## Belief vocabulary

Five classes with per-class decay rates (migration 014):

| Class | Decay λ | Half-life | Use case |
|---|---|---|---|
| `axiom` | 0.0 | ∞ | Permanent truths (core wounds, companion identity) |
| `self_model` | 0.0005 | ~1400d | Companion's introspective facts |
| `user_model` | 0.001 | ~700d | User's psychological profile, wounds, preferences |
| `operational` | 0.003 | ~230d | Day-to-day context, current preferences |
| `hypothesis` | 0.005 | ~140d | Provisional reads awaiting confirmation |

`confidence_floor ∈ [0, 1]` preserves salience against aggressive decay. `archivable=0` pins events against consolidation.

## Quickstart

```bash
# Build
go build -o bin/pulse ./cmd/pulse

# Run bridge
bin/pulse

# Python retrieval
from extract.retrieval_v2 import retrieve_events
events = retrieve_events(con, "query", embedder_model="openai-text-embedding-3-large")
```

## Tests

    python3 -m pytest scripts/tests/ -q
    # 325 passed, 7 skipped

## License

See LICENSE.
