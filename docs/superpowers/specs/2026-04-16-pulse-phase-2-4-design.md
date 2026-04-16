# Pulse Phases 2-4: Tool-Use + Rich Graph + Retrieval — Design Spec

## Problem Statement

Phase 1 scale smoke ($10, 200 jobs) показал: **0 successful writes в граф**. Root cause: Opus без structured output систематически возвращает entities с пустым `canonical_name` (161/161 entity failures = KeyError). Widened except ловит всё cleanly, но блокирует все writes.

Параллельно: текущий граф не используется для contextual retrieval — Elle не может автоматически подгружать релевантный контекст при смене темы разговора.

## Architecture Overview

```
Observations (TG, Claude JSONL, voice)
        │
        ▼
┌─────────────────┐
│  Triage (Sonnet) │  ← tool-use structured verdict
│  extract/skip    │
└───────┬─────────┘
        │ extract
        ▼
┌─────────────────┐
│  Extract (Opus)  │  ← tool-use with JSON schema
│  entities, rels  │     required fields, enum kinds
│  facts, events   │
└───────┬─────────┘
        │
        ▼
┌─────────────────┐
│  Apply + Resolve │  existing confidence gates
│  SAVEPOINT/item  │  + enriched schema
└───────┬─────────┘
        │
        ▼
┌─────────────────┐     ┌──────────────────┐
│   Graph Store    │◄────│  Consolidation   │
│  SQLite tables   │     │  (Sonnet, daily)  │
└───────┬─────────┘     └──────────────────┘
        │
        ▼
┌─────────────────┐     ┌──────────────────┐
│  Retrieval       │◄────│  User message    │
│  keyword → graph │     │  (from Elle/TG)  │
│  → context       │     └──────────────────┘
└─────────────────┘
        │
        ▼ (Phase 3)
┌─────────────────┐
│  Embedding index │  sqlite-vec + OpenAI embeddings
│  semantic search │  auto-context injection
└─────────────────┘
        │
        ▼ (Phase 4)
┌─────────────────┐
│  MCP + Viewer    │  tools, web UI, human-in-the-loop
└─────────────────┘
```

---

## Phase 2: Tool-Use + Rich Schema + Basic Retrieval

### 2.1 Tool-Use Structured Output (blocker fix)

**Problem:** `call_opus_extract` sends a text prompt asking for JSON. Opus returns free-form text with blank fields. `_apply_extraction` does `ent["canonical_name"]` → KeyError → 0 writes.

**Solution:** Anthropic tool-use API с JSON schema. Required fields = Opus physically cannot omit them.

#### Files

| Action | File | What changes |
|--------|------|-------------|
| Create | `scripts/extract/tool_schemas.py` | Tool definitions for triage + extract |
| Modify | `scripts/extract/prompts.py` | Remove `EXTRACT_INSTRUCTIONS` text block, keep `build_extract_prompt` but simplified (observation context only, no "Respond with JSON") |
| Modify | `scripts/pulse_extract.py` | `call_opus_extract` → tool-use API call; `call_sonnet_triage` → tool-use API call; response parsing via `tool_use` content blocks |
| Modify | `scripts/tests/test_extract_phase1.py` | Update fixtures for tool-use response format |
| Modify | `scripts/tests/test_extract_e2e.py` | Update mock responses |
| Create | `scripts/tests/fixtures/tool_use_responses.json` | New fixtures with tool-use format |

#### Tool Schema: Extract

```python
EXTRACT_TOOL = {
    "name": "save_extraction",
    "description": "Save extracted knowledge graph data from the observation",
    "input_schema": {
        "type": "object",
        "required": ["entities", "relations", "events", "facts"],
        "properties": {
            "entities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["canonical_name", "kind"],
                    "properties": {
                        "canonical_name": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Primary name for this entity"
                        },
                        "kind": {
                            "type": "string",
                            "enum": [
                                "person", "place", "project", "org",
                                "product", "community", "skill",
                                "concept", "thing", "event_series"
                            ]
                        },
                        "aliases": {
                            "type": "array",
                            "items": {"type": "string"}
                        },
                        "salience": {
                            "type": "number", "minimum": 0, "maximum": 1,
                            "description": "How important to Nik's life (0-1)"
                        },
                        "emotional_weight": {
                            "type": "number", "minimum": 0, "maximum": 1,
                            "description": "Emotional charge (0=neutral, 1=Anna/therapist-level)"
                        }
                    }
                }
            },
            "relations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["from", "to", "kind"],
                    "properties": {
                        "from": {"type": "string", "minLength": 1},
                        "to": {"type": "string", "minLength": 1},
                        "kind": {
                            "type": "string",
                            "description": "Relationship type: colleague, spouse, friend, uses, member_of, etc."
                        },
                        "context": {
                            "type": "string",
                            "description": "Qualifying context: 'through Cherry Peak', 'from St. Petersburg'"
                        },
                        "strength": {"type": "number", "minimum": 0, "maximum": 1}
                    }
                }
            },
            "events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["title", "entities_involved"],
                    "properties": {
                        "title": {"type": "string", "minLength": 1},
                        "description": {"type": "string"},
                        "sentiment": {"type": "number", "minimum": -1, "maximum": 1},
                        "emotional_weight": {"type": "number", "minimum": 0, "maximum": 1},
                        "ts": {"type": "string", "description": "ISO 8601 timestamp"},
                        "entities_involved": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1
                        }
                    }
                }
            },
            "facts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["entity", "text"],
                    "properties": {
                        "entity": {"type": "string", "minLength": 1},
                        "text": {
                            "type": "string", "minLength": 1,
                            "description": "Atomic factual claim about this entity"
                        },
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1}
                    }
                }
            },
            "merge_candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "new_name": {"type": "string"},
                        "existing_id": {"type": "integer"},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1}
                    }
                }
            }
        }
    }
}
```

#### Tool Schema: Triage

```python
TRIAGE_TOOL = {
    "name": "triage_observations",
    "description": "Classify observations for extraction",
    "input_schema": {
        "type": "object",
        "required": ["verdicts"],
        "properties": {
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["index", "verdict", "reason"],
                    "properties": {
                        "index": {"type": "integer", "minimum": 1},
                        "verdict": {
                            "type": "string",
                            "enum": ["extract", "skip", "defer"]
                        },
                        "reason": {"type": "string"}
                    }
                }
            }
        }
    }
}
```

#### API Call Changes

Current (`pulse_extract.py:call_opus_extract`):
```python
msg = client.messages.create(
    model=EXTRACT_MODEL,
    max_tokens=4096,
    messages=[{"role": "user", "content": prompt}],
)
text = "".join(block.text for block in msg.content if hasattr(block, "text"))
return prompts.parse_extract_response(text)
```

New:
```python
msg = client.messages.create(
    model=EXTRACT_MODEL,
    max_tokens=4096,
    messages=[{"role": "user", "content": prompt}],
    tools=[tool_schemas.EXTRACT_TOOL],
    tool_choice={"type": "tool", "name": "save_extraction"},
)
for block in msg.content:
    if block.type == "tool_use" and block.name == "save_extraction":
        return block.input  # already dict, already validated by schema
raise ValueError("Opus did not call save_extraction tool")
```

**Key insight:** `tool_choice={"type": "tool", "name": "save_extraction"}` forces Opus to call the tool. Schema validation happens server-side. `block.input` is already a parsed dict with all required fields populated.

### 2.2 Rich Schema (Migration 007)

#### Schema Changes

```sql
-- 007_phase_2.sql

-- Expand entity kind enum
-- SQLite CHECK constraints can't be ALTERed, so:
-- 1. Create new table with expanded CHECK
-- 2. Copy data
-- 3. Drop old, rename new

CREATE TABLE entities_new (
    id                INTEGER PRIMARY KEY,
    canonical_name    TEXT NOT NULL,
    kind              TEXT NOT NULL CHECK(kind IN (
        'person','place','project','org','product',
        'community','skill','concept','thing','event_series'
    )),
    aliases           TEXT,
    first_seen        TEXT NOT NULL,
    last_seen         TEXT NOT NULL,
    salience_score    REAL NOT NULL DEFAULT 0,
    emotional_weight  REAL NOT NULL DEFAULT 0,
    scorer_version    TEXT,
    description_md    TEXT
);

INSERT INTO entities_new SELECT * FROM entities;
DROP TABLE entities;
ALTER TABLE entities_new RENAME TO entities;

-- Rebuild indexes
CREATE INDEX idx_entities_kind ON entities(kind);

-- Relation context
ALTER TABLE relations ADD COLUMN context TEXT;

-- Fact verification
ALTER TABLE facts ADD COLUMN verified INTEGER NOT NULL DEFAULT 0;
ALTER TABLE facts ADD COLUMN verified_by TEXT;
```

**New entity kinds (4 additions):**

| Kind | What it covers | Example |
|------|---------------|---------|
| `product` | Tools, services, apps | Claude, Cursor, Telegram |
| `community` | Groups, forums, channels | TG groups, Discord servers |
| `skill` | Technologies, languages, capabilities | Go, React, IFS therapy |
| `concept` | Ideas, patterns, architectural decisions | Mirror layer, salience scoring |

### 2.3 Basic Retrieval

**Goal:** Given a user message, return relevant graph context for Elle's response.

#### File: `scripts/extract/retrieval.py`

```python
def retrieve_context(con: sqlite3.Connection, message: str, top_k: int = 10) -> dict:
    """Keyword-based graph retrieval.
    
    1. Tokenize message into candidate names
    2. Exact match against entities.canonical_name + aliases
    3. 1-hop relation traversal from matched entities
    4. Collect facts for matched entities
    5. Rank by salience_score * recency
    6. Return top-K entities with their relations + facts
    """
```

#### Query Flow

```
User message: "давай обсудим переезд в Хуахин"
                │
                ▼
    Tokenize: ["переезд", "Хуахин"]
                │
                ▼
    Match entities: Хуахин (place, id=42)
                │
                ▼
    1-hop relations:
      - Ник → Хуахин (kind="potential_relocation", context="2026 plan")
      - Аня → Хуахин (kind="co-relocator")
                │
                ▼
    Facts for entity 42:
      - "Average rent 2BR: 25,000 THB/mo" (confidence=0.8)
      - "Direct flights from Moscow: none" (confidence=0.9)
                │
                ▼
    Context payload → Elle's prompt
```

#### Return Format

```python
{
    "matched_entities": [
        {
            "id": 42,
            "canonical_name": "Хуахин",
            "kind": "place",
            "salience_score": 0.7,
            "facts": [...],
            "relations": [
                {"other_entity": "Nik", "kind": "potential_relocation", "context": "2026 plan"},
                {"other_entity": "Anna", "kind": "co-relocator", "context": ""},
            ]
        }
    ],
    "total_matched": 1,
    "retrieval_method": "keyword"
}
```

### 2.4 Consolidation

**File:** `scripts/pulse_consolidate.py`

**Model:** Sonnet (cheap, fast)

**Schedule:** Daily cron or manual trigger

#### What it does

1. **Duplicate detection**: Query entities with similar names (SequenceMatcher > 0.8), propose merges
2. **Stale open_questions**: Auto-close questions past TTL
3. **Entity stats**: Count entities by kind, flag orphans (no relations, no facts)
4. **Merge execution**: Process approved merge_proposals (merge entities, repoint relations/facts/evidence)
5. **Report**: Print summary to stdout (for cron logging)

#### Sonnet prompt (consolidation review)

```
Given these entity pairs with similarity > 0.8, decide which should be merged:
[pairs list]

For each pair, respond:
- MERGE: they are the same entity (keep the one with more evidence)
- KEEP: they are distinct entities
- UNSURE: need human review
```

### 2.5 Success Criteria (Phase 2)

| Criterion | Metric |
|-----------|--------|
| Writes unblocked | >80% of extracted entities written successfully (vs 0% currently) |
| Tool-use validation | 0 KeyError on canonical_name in 100+ jobs |
| Rich kinds | Entities created with all 10 kind values in real data |
| Relation context | >50% of relations have non-empty context |
| Retrieval latency | <500ms for keyword retrieval on 10K entity graph |
| Consolidation | Daily run completes <60s, produces actionable merge proposals |
| Test coverage | 70+ tests passing (currently 60) |

---

## Phase 3: Semantic Retrieval + Embedding Index

### 3.1 Problem

Keyword retrieval (Phase 2) fails when:
- User says "моя жена" but entity is "Аня" (no keyword match)
- User discusses concept by description, not name ("та штука с графом знаний" → Pulse)
- Multilingual aliases ("Hua Hin" vs "Хуахин")

### 3.2 Architecture

```
User message
     │
     ▼
┌────────────────┐
│ Embed message   │  OpenAI text-embedding-3-large
│ (1536-dim)      │
└───────┬────────┘
        │
        ▼
┌────────────────┐     ┌──────────────────┐
│ Vector search   │────►│ sqlite-vec index  │
│ top-K entities  │     │ entity embeddings │
└───────┬────────┘     └──────────────────┘
        │
        ▼
┌────────────────┐
│ Graph expansion │  1-hop relations + facts
│ from top-K      │  for matched entities
└───────┬────────┘
        │
        ▼
┌────────────────┐
│ Rank + Trim     │  salience * vector_score * recency
│ → context       │  fit within context window budget
└────────────────┘
```

### 3.3 What Gets Embedded

| Object | Embed text | When | Storage |
|--------|-----------|------|---------|
| Entity | `"{canonical_name} ({kind}): {description_md}. Aliases: {aliases}. Key facts: {top-3 facts}"` | On create + on fact/relation change (batch, not real-time) | `entity_embeddings(entity_id PK, embedding BLOB, updated_at)` |
| Fact | Not separately — rolled into entity embedding | — | — |
| Relation | Not separately — described in entity context | — | — |
| Event | `"{title}: {description}. Entities: {involved}. Sentiment: {sentiment}"` | On create | `event_embeddings(event_id PK, embedding BLOB, updated_at)` |

**Rationale:** Embedding per-entity (not per-fact) keeps the index small. With 10K entities × 1536 dims × 4 bytes = ~60MB — fits in memory. Facts and relations enrich the entity embedding text but don't need their own vectors.

### 3.4 Embedding Pipeline

#### Schema (Migration 008)

```sql
-- 008_embeddings.sql

CREATE VIRTUAL TABLE entity_vec USING vec0(
    entity_id INTEGER PRIMARY KEY,
    embedding float[1536]
);

CREATE VIRTUAL TABLE event_vec USING vec0(
    event_id INTEGER PRIMARY KEY,
    embedding float[1536]
);

CREATE TABLE embedding_metadata (
    subject_kind TEXT NOT NULL CHECK(subject_kind IN ('entity','event')),
    subject_id   INTEGER NOT NULL,
    model        TEXT NOT NULL,
    text_hash    TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (subject_kind, subject_id)
);
```

#### File: `scripts/extract/embeddings.py`

```python
def embed_entities_batch(con, entity_ids: list[int], client: OpenAI) -> int:
    """Embed or re-embed entities. Returns count of embeddings written."""

def embed_events_batch(con, event_ids: list[int], client: OpenAI) -> int:
    """Embed or re-embed events."""

def needs_reembed(con, subject_kind: str, subject_id: int, current_text: str) -> bool:
    """Check if text_hash changed since last embedding."""
```

#### Batch Schedule

- **On extraction:** After `_apply_extraction` writes entities → queue entity IDs for embedding
- **Batch job:** `scripts/pulse_embed.py --db <path>` — process queue, embed all dirty entities
- **Cron:** Run after `pulse_consolidate.py` (consolidation may merge entities → reembed)

#### Cost Estimate

OpenAI text-embedding-3-large: $0.13/1M tokens.
- 10K entities × ~200 tokens each = 2M tokens = **$0.26** for full re-embed
- Daily incremental: ~50-100 new entities × 200 tokens = negligible

### 3.5 Retrieval Query Flow

#### File: `scripts/extract/retrieval.py` (extended)

```python
def retrieve_context_semantic(
    con: sqlite3.Connection,
    message: str,
    client: OpenAI,
    top_k: int = 10,
    context_budget_tokens: int = 2000,
) -> dict:
    """Hybrid retrieval: keyword first, then semantic.
    
    1. Keyword match (Phase 2) — free, instant
    2. If <top_k results: embed message → vector search entity_vec
    3. Merge results, deduplicate
    4. Graph expansion: 1-hop relations + top facts per entity
    5. Rank: salience * vector_score * recency_decay
    6. Trim to context_budget_tokens
    """
```

#### Hybrid Strategy

```
Message → keyword_results (free, <10ms)
              │
              ├── len >= top_k? → done, return
              │
              └── len < top_k? → embed message ($0.00001)
                                      │
                                      ▼
                                 vector search
                                 entity_vec + event_vec
                                      │
                                      ▼
                                 merge + dedup
                                      │
                                      ▼
                                 graph expansion
                                      │
                                      ▼
                                 rank + trim → return
```

### 3.6 Alias Index

#### Schema (in Migration 008)

```sql
CREATE TABLE entity_aliases (
    id        INTEGER PRIMARY KEY,
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    alias     TEXT NOT NULL COLLATE NOCASE,
    source    TEXT NOT NULL CHECK(source IN ('extraction','manual','merge','consolidation')),
    added_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_aliases_unique ON entity_aliases(alias, entity_id);
CREATE INDEX idx_aliases_text ON entity_aliases(alias COLLATE NOCASE);
```

**Currently:** aliases stored as JSON array in `entities.aliases` column — not queryable for retrieval.
**Phase 3:** Normalized `entity_aliases` table with NOCASE index for fast lookup. Extraction writes both: JSON array (backward compat) + alias rows.

### 3.7 Merge Confirmation Flow

Basic CLI + Telegram flow (no web UI yet):

1. `pulse_consolidate.py` creates `entity_merge_proposals` with state='pending'
2. Daily report sent to Nik via Telegram outbox: "3 merge proposals pending: Anna/Аня (0.92), Хуахин/Hua Hin (0.95)..."
3. Nik replies: "merge 1,2" or "reject 3"
4. Bridge parses → updates proposal state → runs merge
5. Merged entities get re-embedded

#### File: `scripts/pulse_merge.py`

```python
def execute_merge(con, from_entity_id: int, to_entity_id: int) -> dict:
    """Merge from_entity into to_entity.
    
    - Repoint all relations, facts, evidence, event_entities
    - Merge aliases (union)
    - Update salience (max)
    - Delete from_entity
    - Mark proposal as approved
    - Queue to_entity for re-embedding
    """
```

### 3.8 Success Criteria (Phase 3)

| Criterion | Metric |
|-----------|--------|
| Semantic match | "моя жена" retrieves Anna entity with >0.7 cosine similarity |
| Topic switch latency | <2s from message to context payload (embed + search + expansion) |
| Hybrid fallback | Keyword-only queries don't call embedding API (cost = $0) |
| Alias coverage | >90% of entities have >=1 alias in normalized table |
| Merge flow | End-to-end merge via Telegram: proposal → approval → execution |
| Embedding freshness | Dirty entities re-embedded within 1 consolidation cycle |
| Index size | <100MB for 10K entities + 5K events |

---

## Phase 4: MCP + Web Viewer + Human-in-the-Loop

### 4.1 MCP Tools for Graph Query

**Goal:** Elle and other agents query the graph via MCP protocol — no direct DB access needed.

#### File: `scripts/mcp_graph_server.py`

MCP server exposing graph operations as tools:

| Tool | Input | Output |
|------|-------|--------|
| `graph_search` | `{query: str, top_k: int}` | Entities + relations + facts (uses hybrid retrieval) |
| `graph_entity` | `{entity_id: int}` | Full entity profile: facts, relations, events, evidence |
| `graph_relations` | `{entity_id: int, direction: "from"\|"to"\|"both"}` | All relations for entity |
| `graph_facts` | `{entity_id: int, verified_only: bool}` | Facts for entity |
| `graph_timeline` | `{entity_id: int, since: str, until: str}` | Events involving entity in time range |
| `graph_stats` | `{}` | Entity count by kind, relation count, fact count, last extraction |
| `graph_merge_pending` | `{}` | Pending merge proposals |
| `graph_merge_approve` | `{proposal_id: int}` | Execute merge |
| `graph_merge_reject` | `{proposal_id: int}` | Reject merge |
| `graph_verify_fact` | `{fact_id: int, verified_by: str}` | Mark fact as verified |

#### Integration with Elle

In Elle's CLAUDE.md or `.mcp.json`:
```json
{
  "mcpServers": {
    "pulse-graph": {
      "command": "python3",
      "args": ["scripts/mcp_graph_server.py", "--db", "/path/to/pulse.db"]
    }
  }
}
```

Elle can now: "Let me check what I know about this person" → `graph_search({query: "Даша Cherry Peak"})` → structured context.

### 4.2 Web Viewer (Read-Only Dashboard)

**Goal:** Nik can browse the knowledge graph in a browser.

#### File: `scripts/graph_viewer.py` (lightweight Flask/FastAPI)

**Views:**
- `/` — Dashboard: entity count by kind, recent events, pending merges
- `/entities` — Searchable entity list with filters by kind
- `/entity/<id>` — Entity detail: facts, relations graph, event timeline, evidence chain
- `/graph` — Force-directed graph visualization (D3.js or similar)
- `/merges` — Pending merge proposals with approve/reject buttons
- `/facts?unverified=true` — Unverified facts for review

**Stack:** Python + Jinja2 templates + htmx (no SPA framework). SQLite read-only connection.

### 4.3 Human-in-the-Loop Refinement

#### Fact Verification Flow

1. During extraction: facts written with `verified=0`
2. Web viewer `/facts?unverified=true` shows unverified facts
3. Nik clicks "verify" / "reject" / "edit"
4. Or via Telegram: daily digest "5 new facts about Anna — correct?" → Nik confirms/corrects
5. Verified facts get higher weight in retrieval ranking

#### Entity Type Suggestions

1. Consolidation script detects entities that don't fit existing kinds well
2. Proposes new kind: "Found 15 entities tagged 'thing' that look like 'recipe' — add new kind?"
3. Nik approves → migration adds new CHECK value → extraction starts using it

### 4.4 Dynamic Entity Type Discovery

**Model:** Sonnet

**Trigger:** Weekly consolidation review

```python
def suggest_new_kinds(con) -> list[dict]:
    """Analyze entities tagged 'thing' or 'concept' for clusters.
    
    If >10 entities share a pattern (e.g., all are food items),
    suggest a new kind with examples.
    """
```

Decision: Nik approves/rejects via Telegram or web viewer.
If approved: schema migration generated automatically (ALTER TABLE workaround for CHECK constraint).

### 4.5 Success Criteria (Phase 4)

| Criterion | Metric |
|-----------|--------|
| MCP integration | Elle uses graph_search in >50% of contextual responses |
| Web viewer | Dashboard loads in <3s with 10K entities |
| Fact verification | >30% of facts verified within 2 weeks of creation |
| Merge throughput | Average merge proposal resolved within 48h |
| Dynamic kinds | At least 1 new kind added via discovery flow |

---

## Roadmap: Dependencies & Effort

```
Phase 2a (blocker)     Phase 2b (enrich)      Phase 2c (retrieve)    Phase 2d (consolidate)
Tool-use schema        Migration 007          retrieval.py           consolidate.py
tool_schemas.py        Rich kinds             keyword matching       Sonnet review
pulse_extract.py mod   relation context       1-hop traversal        merge execution
fixture updates        fact verified flag                            daily cron
     │                      │                       │                      │
     └──── must complete ───┘                       │                      │
           before 2b                                │                      │
                                                    └──── can parallel ────┘
                                                          with 2a/2b

Phase 3a (embed)       Phase 3b (semantic)     Phase 3c (aliases+merge)
Migration 008          Hybrid retrieval        entity_aliases table
embeddings.py          vector search           merge flow (TG)
pulse_embed.py         context ranking         pulse_merge.py
                       budget trimming
     │                      │                       │
     └──── depends on ──────┘                       │
           Phase 2 complete                         └── can start with 3a
                                                        (normalized aliases)

Phase 4a (MCP)         Phase 4b (viewer)       Phase 4c (HITL)
mcp_graph_server.py    graph_viewer.py         TG digest flow
.mcp.json config       templates + htmx        fact verification
                       D3 graph viz            dynamic kinds
     │                      │                       │
     └── depends on Phase 3 (retrieval works) ──────┘
```

### Effort Estimates

| Phase | Estimated tasks | Effort | Dependencies |
|-------|----------------|--------|-------------|
| **2a** Tool-use schema | 4-5 tasks | 1 session | None (blocker) |
| **2b** Rich schema | 3-4 tasks | 1 session | 2a (needs working writes) |
| **2c** Basic retrieval | 3-4 tasks | 1 session | 2a (needs data in graph) |
| **2d** Consolidation | 3-4 tasks | 1 session | 2a, can parallel with 2c |
| **3a** Embedding pipeline | 4-5 tasks | 1 session | Phase 2 complete |
| **3b** Semantic retrieval | 3-4 tasks | 1 session | 3a |
| **3c** Alias index + merge | 3-4 tasks | 1 session | 3a, can parallel with 3b |
| **4a** MCP server | 3-4 tasks | 1 session | Phase 3 complete |
| **4b** Web viewer | 5-6 tasks | 1-2 sessions | Phase 3 complete |
| **4c** Human-in-the-loop | 4-5 tasks | 1 session | 4a + 4b |

**Total: ~35-45 tasks across 3 phases, ~8-12 sessions.**

### Parallelization Opportunities

- **2c + 2d** can run in parallel (both depend on 2a/2b but not each other)
- **3b + 3c** can run in parallel (both depend on 3a but not each other)
- **4a + 4b** can run in parallel after Phase 3

### Immediate Next Step

**Phase 2a (tool-use schema)** — unblocks everything. 4-5 tasks, 1 session. After this, we can run scale smoke again and expect >80% successful writes instead of 0%.
