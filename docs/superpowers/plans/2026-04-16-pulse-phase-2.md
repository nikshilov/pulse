# Pulse Phase 2: Tool-Use + Rich Schema + Retrieval — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the 0% write rate by switching to Anthropic tool-use structured output, enrich the graph schema with provenance and 10 entity kinds, add keyword retrieval and a Sonnet consolidation pipeline.

**Architecture:** Replace free-text LLM prompts with forced tool-use (`tool_choice`), so Opus returns structured dicts with required fields. Enrich the schema (migration 007) with relation context, fact provenance, extraction metrics. Add `retrieval.py` for keyword-based graph queries and `pulse_consolidate.py` for periodic dedup/cleanup.

**Tech Stack:** Python 3.13, Anthropic SDK (tool-use API), SQLite (CHECK constraints, FTS-like keyword matching), pytest + monkeypatch.

**Working directory:** `~/dev/ai/pulse/scripts/` (all imports are relative to this dir)

**Test runner:** `cd ~/dev/ai/pulse/scripts && python -m pytest tests/ -v`

**Spec:** `~/dev/ai/pulse/docs/superpowers/specs/2026-04-16-pulse-phase-2-4-design.md`

---

## File Structure

### New Files

| File | Responsibility |
|------|---------------|
| `scripts/extract/tool_schemas.py` | EXTRACT_TOOL and TRIAGE_TOOL schema dicts for Anthropic tool-use API |
| `scripts/extract/retrieval.py` | Keyword-based graph retrieval: tokenize → match entities → 1-hop expansion → rank |
| `scripts/pulse_consolidate.py` | Consolidation CLI: find duplicates, close stale questions, process approved merges, report stats |
| `internal/store/migrations/007_phase_2.sql` | Expand entity kinds to 10, add relation.context, fact provenance, extraction_metrics table |
| `scripts/tests/test_tool_schemas.py` | Validate tool schema structure |
| `scripts/tests/test_retrieval.py` | Keyword retrieval unit tests |
| `scripts/tests/test_consolidate.py` | Consolidation pipeline tests |

### Modified Files

| File | What changes |
|------|-------------|
| `scripts/extract/prompts.py` | Simplify EXTRACT_INSTRUCTIONS and TRIAGE_INSTRUCTIONS (remove formatting directives), remove `parse_extract_response` and `parse_triage_response` |
| `scripts/pulse_extract.py` | `call_opus_extract` and `call_sonnet_triage` → tool-use API; `_apply_extraction` adds relation.context, fact provenance; `run_once` saves extraction_metrics |
| `scripts/tests/test_extract_phase1.py` | Add schema tests for migration 007 columns; update mock returns to tuple `(data, usage)` |
| `scripts/tests/test_extract_e2e.py` | Update mock returns to tuple format |
| `scripts/tests/test_extract_prompt.py` | Remove tests for deleted parse functions; update prompt tests for simplified instructions |
| `scripts/tests/test_triage_prompt.py` | Remove tests for deleted parse function; update for simplified instructions |
| `scripts/tests/test_extract_loop.py` | Update mock returns to tuple format |

---

### Task 1: Migration 007 — Rich Schema + Metrics

**Files:**
- Create: `internal/store/migrations/007_phase_2.sql`
- Modify: `scripts/tests/test_extract_phase1.py` (add schema validation tests)

- [ ] **Step 1: Write the migration SQL**

Create `internal/store/migrations/007_phase_2.sql`:

```sql
-- 007_phase_2.sql
-- Phase 2: Expanded entity kinds (10), relation context, fact provenance, extraction metrics

-- SQLite CHECK can't be ALTERed — recreate entities table with expanded enum
PRAGMA foreign_keys = OFF;

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
    description_md    TEXT,
    extractor_version TEXT NOT NULL DEFAULT 'v1'
);

INSERT INTO entities_new (id, canonical_name, kind, aliases, first_seen, last_seen,
    salience_score, emotional_weight, scorer_version, description_md, extractor_version)
SELECT id, canonical_name, kind, aliases, first_seen, last_seen,
    salience_score, emotional_weight, scorer_version, description_md, 'v1'
FROM entities;

DROP TABLE entities;
ALTER TABLE entities_new RENAME TO entities;

CREATE INDEX idx_entities_kind ON entities(kind);

PRAGMA foreign_keys = ON;

-- Relation context (qualifying info: "through Cherry Peak", "from St. Petersburg")
ALTER TABLE relations ADD COLUMN context TEXT;

-- Fact verification + provenance
ALTER TABLE facts ADD COLUMN verified INTEGER NOT NULL DEFAULT 0;
ALTER TABLE facts ADD COLUMN verified_by TEXT;
ALTER TABLE facts ADD COLUMN source_obs_id INTEGER REFERENCES observations(id);
ALTER TABLE facts ADD COLUMN extractor_version TEXT NOT NULL DEFAULT 'v1';

-- Extraction cost/performance tracking
CREATE TABLE extraction_metrics (
    id            INTEGER PRIMARY KEY,
    job_id        INTEGER NOT NULL REFERENCES extraction_jobs(id),
    model         TEXT NOT NULL,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cost_usd      REAL,
    latency_ms    INTEGER,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
```

- [ ] **Step 2: Write schema validation tests**

Add to `scripts/tests/test_extract_phase1.py` (append after existing schema tests around line 143):

```python
# --- Migration 007 schema tests ---

def test_migration_007_entities_accepts_new_kinds(tmp_path):
    con = _fresh_db(tmp_path)
    now = "2026-04-16T00:00:00Z"
    new_kinds = ["product", "community", "skill", "concept"]
    for kind in new_kinds:
        con.execute(
            "INSERT INTO entities (canonical_name, kind, first_seen, last_seen) VALUES (?,?,?,?)",
            (f"test_{kind}", kind, now, now),
        )
    rows = con.execute("SELECT kind FROM entities ORDER BY kind").fetchall()
    assert set(r[0] for r in rows) == set(new_kinds)


def test_migration_007_entities_rejects_invalid_kind(tmp_path):
    con = _fresh_db(tmp_path)
    import sqlite3 as _s
    with pytest.raises(_s.IntegrityError):
        con.execute(
            "INSERT INTO entities (canonical_name, kind, first_seen, last_seen) VALUES (?,?,?,?)",
            ("bad", "invalid_kind", "2026-01-01", "2026-01-01"),
        )


def test_migration_007_entities_has_extractor_version(tmp_path):
    con = _fresh_db(tmp_path)
    con.execute(
        "INSERT INTO entities (canonical_name, kind, first_seen, last_seen, extractor_version) VALUES (?,?,?,?,?)",
        ("Nik", "person", "2026-01-01", "2026-01-01", "v2"),
    )
    row = con.execute("SELECT extractor_version FROM entities WHERE canonical_name='Nik'").fetchone()
    assert row[0] == "v2"


def test_migration_007_relations_has_context(tmp_path):
    con = _fresh_db(tmp_path)
    now = "2026-04-16T00:00:00Z"
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (1,'A','person',?,?)", (now, now))
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (2,'B','person',?,?)", (now, now))
    con.execute(
        "INSERT INTO relations (from_entity_id, to_entity_id, kind, strength, first_seen, last_seen, context) VALUES (1,2,'colleague',1.0,?,?,'through Cherry Peak')",
        (now, now),
    )
    row = con.execute("SELECT context FROM relations WHERE from_entity_id=1").fetchone()
    assert row[0] == "through Cherry Peak"


def test_migration_007_facts_has_provenance(tmp_path):
    con = _fresh_db(tmp_path)
    now = "2026-04-16T00:00:00Z"
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (1,'A','person',?,?)", (now, now))
    con.execute(
        "INSERT INTO facts (entity_id, text, confidence, source_obs_id, extractor_version, verified, created_at) VALUES (1,'fact text',0.9,42,'v2',0,?)",
        (now,),
    )
    row = con.execute("SELECT source_obs_id, extractor_version, verified FROM facts WHERE entity_id=1").fetchone()
    assert row == (42, "v2", 0)


def test_migration_007_extraction_metrics_table(tmp_path):
    con = _fresh_db(tmp_path)
    con.execute(
        "INSERT INTO extraction_jobs (observation_ids, state, attempts, created_at) VALUES ('[]','done',1,'2026-01-01')"
    )
    job_id = con.execute("SELECT id FROM extraction_jobs").fetchone()[0]
    con.execute(
        "INSERT INTO extraction_metrics (job_id, model, input_tokens, output_tokens, cost_usd, latency_ms) VALUES (?,?,?,?,?,?)",
        (job_id, "claude-opus-4-6", 1000, 500, 0.15, 3200),
    )
    row = con.execute("SELECT model, input_tokens, output_tokens FROM extraction_metrics WHERE job_id=?", (job_id,)).fetchone()
    assert row == ("claude-opus-4-6", 1000, 500)
```

Note: `_fresh_db` is a helper that creates a DB and applies all migrations. If it doesn't exist, use this pattern from existing tests:

```python
def _fresh_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    migrations_dir = Path(__file__).resolve().parent.parent.parent / "internal" / "store" / "migrations"
    for sql_file in sorted(migrations_dir.glob("*.sql")):
        con.executescript(sql_file.read_text())
    return con
```

- [ ] **Step 3: Run tests to verify they pass**

Run: `cd ~/dev/ai/pulse/scripts && python -m pytest tests/test_extract_phase1.py -v -k "migration_007"`

Expected: 6 new tests PASS.

- [ ] **Step 4: Run full test suite to verify no regressions**

Run: `cd ~/dev/ai/pulse/scripts && python -m pytest tests/ -v`

Expected: All existing tests PASS (migration 007 is additive — new columns have defaults, new table is new).

- [ ] **Step 5: Commit**

```bash
cd ~/dev/ai/pulse
git add internal/store/migrations/007_phase_2.sql scripts/tests/test_extract_phase1.py
git commit -m "feat: migration 007 — expand entity kinds, add provenance, extraction_metrics"
```

---

### Task 2: Tool Schema Definitions

**Files:**
- Create: `scripts/extract/tool_schemas.py`
- Create: `scripts/tests/test_tool_schemas.py`

- [ ] **Step 1: Write the tool schema file**

Create `scripts/extract/tool_schemas.py`:

```python
"""Anthropic tool-use schemas for triage and extraction."""

ENTITY_KINDS = [
    "person", "place", "project", "org", "product",
    "community", "skill", "concept", "thing", "event_series",
]

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
                            "description": "Primary name for this entity",
                        },
                        "kind": {
                            "type": "string",
                            "enum": ENTITY_KINDS,
                        },
                        "aliases": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "salience": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                            "description": "How important to Nik's life (0-1)",
                        },
                        "emotional_weight": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                            "description": "Emotional charge (0=neutral, 1=Anna/therapist-level)",
                        },
                    },
                },
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
                            "description": "Relationship type: colleague, spouse, friend, uses, member_of, etc.",
                        },
                        "context": {
                            "type": "string",
                            "description": "Qualifying context: 'through Cherry Peak', 'from St. Petersburg'",
                        },
                        "strength": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                },
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
                            "minItems": 1,
                        },
                    },
                },
            },
            "facts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["entity", "text"],
                    "properties": {
                        "entity": {"type": "string", "minLength": 1},
                        "text": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Atomic factual claim about this entity",
                        },
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                },
            },
            "merge_candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "new_name": {"type": "string"},
                        "existing_id": {"type": "integer"},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                },
            },
        },
    },
}

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
                            "enum": ["extract", "skip", "defer"],
                        },
                        "reason": {"type": "string"},
                    },
                },
            },
        },
    },
}
```

- [ ] **Step 2: Write schema validation tests**

Create `scripts/tests/test_tool_schemas.py`:

```python
"""Validate tool schema structure for Anthropic API."""

from extract.tool_schemas import EXTRACT_TOOL, TRIAGE_TOOL, ENTITY_KINDS


def test_extract_tool_has_required_top_level_keys():
    assert EXTRACT_TOOL["name"] == "save_extraction"
    assert "input_schema" in EXTRACT_TOOL
    schema = EXTRACT_TOOL["input_schema"]
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"entities", "relations", "events", "facts"}


def test_extract_tool_entity_has_required_fields():
    entity_schema = EXTRACT_TOOL["input_schema"]["properties"]["entities"]["items"]
    assert set(entity_schema["required"]) == {"canonical_name", "kind"}
    assert entity_schema["properties"]["kind"]["enum"] == ENTITY_KINDS


def test_extract_tool_entity_kind_enum_has_10_values():
    assert len(ENTITY_KINDS) == 10
    assert "person" in ENTITY_KINDS
    assert "product" in ENTITY_KINDS
    assert "community" in ENTITY_KINDS
    assert "skill" in ENTITY_KINDS
    assert "concept" in ENTITY_KINDS


def test_triage_tool_has_required_structure():
    assert TRIAGE_TOOL["name"] == "triage_observations"
    verdict_schema = TRIAGE_TOOL["input_schema"]["properties"]["verdicts"]["items"]
    assert set(verdict_schema["required"]) == {"index", "verdict", "reason"}
    assert verdict_schema["properties"]["verdict"]["enum"] == ["extract", "skip", "defer"]


def test_extract_tool_relation_has_context_field():
    rel_schema = EXTRACT_TOOL["input_schema"]["properties"]["relations"]["items"]
    assert "context" in rel_schema["properties"]


def test_extract_tool_fact_has_required_fields():
    fact_schema = EXTRACT_TOOL["input_schema"]["properties"]["facts"]["items"]
    assert set(fact_schema["required"]) == {"entity", "text"}
```

- [ ] **Step 3: Run tests**

Run: `cd ~/dev/ai/pulse/scripts && python -m pytest tests/test_tool_schemas.py -v`

Expected: 6 tests PASS.

- [ ] **Step 4: Commit**

```bash
cd ~/dev/ai/pulse
git add scripts/extract/tool_schemas.py scripts/tests/test_tool_schemas.py
git commit -m "feat: add tool-use schemas for triage and extraction"
```

---

### Task 3: Simplify Prompts for Tool-Use

**Files:**
- Modify: `scripts/extract/prompts.py`
- Modify: `scripts/tests/test_extract_prompt.py`
- Modify: `scripts/tests/test_triage_prompt.py`

- [ ] **Step 1: Simplify TRIAGE_INSTRUCTIONS and remove parse_triage_response**

In `scripts/extract/prompts.py`, replace the entire file content:

```python
"""Prompts for two-pass extractor: Sonnet triage + Opus extract.

With Phase 2 tool-use, the model output structure is enforced by tool schemas.
These prompts provide semantic guidance — WHAT to extract, not HOW to format it.
"""


TRIAGE_INSTRUCTIONS = """You are the triage filter for a personal knowledge-graph extraction pipeline.

For each numbered observation, classify it using the triage_observations tool.

Verdicts:
- extract: contains people, places, projects, emotions, decisions, or meaningful events
- skip: trivial content (greetings, emoji-only, tool output, mechanical noise)
- defer: ambiguous, needs more context — will be retried later

Be aggressive about extracting — when in doubt, choose extract over skip.
Only skip truly empty observations.
"""


def build_triage_prompt(observations) -> str:
    lines = [TRIAGE_INSTRUCTIONS, "", "Observations:"]
    for i, obs in enumerate(observations, 1):
        actors = ", ".join(
            f"{a.get('kind', '?')}:{a.get('id', '?')}"
            for a in obs.get("actors", [])
        )
        text = (obs.get("content_text") or "").replace("\n", " ")[:500]
        lines.append(f"{i}. [{obs.get('source_kind')} | {actors}] {text}")
    lines.append("")
    lines.append("Classify each observation now.")
    return "\n".join(lines)


EXTRACT_INSTRUCTIONS = """You are the knowledge-graph extractor for a personal AI assistant.

Given an observation from someone's life (chat message, voice memo, meeting note),
extract structured knowledge using the save_extraction tool.

Extract:
- entities: people, places, projects, organizations, products, communities, skills, concepts
- relations: connections between entities, with qualifying context
- events: notable happenings with timestamps when available
- facts: atomic claims about entities with confidence scores
- merge_candidates: if an extracted entity might match an existing one

Scoring guidance:
- salience (0-1): how important is this entity to the person's life
- emotional_weight (0-1): how emotionally charged (0=neutral, 1=therapist-level)
- sentiment (-1..1): positive/negative valence of events

Ground every extraction in the observation's content. Don't hallucinate.
If a name matches an existing entity alias, prefer the existing entity.
"""


def build_extract_prompt(observation: dict, graph_context: dict) -> str:
    existing = graph_context.get("existing_entities", [])
    existing_lines = []
    for e in existing:
        aliases = ", ".join(e.get("aliases") or [])
        existing_lines.append(
            f"  - id={e['id']} name={e['canonical_name']} kind={e['kind']} aliases=[{aliases}]"
        )

    actors = ", ".join(
        f"{a.get('kind')}:{a.get('id')}" for a in observation.get("actors", [])
    )
    return "\n".join([
        EXTRACT_INSTRUCTIONS,
        "",
        "Existing entities in the graph:",
        "\n".join(existing_lines) if existing_lines else "  (none)",
        "",
        f"Observation (source={observation.get('source_kind')}, actors={actors}):",
        observation.get("content_text", ""),
    ])
```

- [ ] **Step 2: Update test_extract_prompt.py**

Replace `scripts/tests/test_extract_prompt.py` content:

```python
"""Tests for extraction prompt builder."""

from extract.prompts import build_extract_prompt, EXTRACT_INSTRUCTIONS


def test_build_extract_prompt_includes_graph_context():
    obs = {"source_kind": "telegram", "actors": [{"kind": "user", "id": "123"}], "content_text": "Hello world"}
    ctx = {"existing_entities": [{"id": 1, "canonical_name": "Anna", "kind": "person", "aliases": ["Аня"]}]}
    prompt = build_extract_prompt(obs, ctx)
    assert "id=1 name=Anna kind=person aliases=[Аня]" in prompt
    assert "Hello world" in prompt


def test_build_extract_prompt_handles_empty_graph():
    obs = {"source_kind": "voice", "actors": [], "content_text": "Test content"}
    ctx = {"existing_entities": []}
    prompt = build_extract_prompt(obs, ctx)
    assert "(none)" in prompt
    assert "Test content" in prompt


def test_extract_instructions_no_json_formatting_directives():
    assert "JSON" not in EXTRACT_INSTRUCTIONS
    assert "```" not in EXTRACT_INSTRUCTIONS
    assert "save_extraction" in EXTRACT_INSTRUCTIONS
```

- [ ] **Step 3: Update test_triage_prompt.py**

Replace `scripts/tests/test_triage_prompt.py` content:

```python
"""Tests for triage prompt builder."""

from extract.prompts import build_triage_prompt, TRIAGE_INSTRUCTIONS


def test_build_triage_prompt_includes_all_observations():
    obs = [
        {"source_kind": "telegram", "actors": [{"kind": "user", "id": "1"}], "content_text": "First msg"},
        {"source_kind": "telegram", "actors": [{"kind": "user", "id": "2"}], "content_text": "Second msg"},
    ]
    prompt = build_triage_prompt(obs)
    assert "1. [telegram" in prompt
    assert "2. [telegram" in prompt
    assert "First msg" in prompt
    assert "Second msg" in prompt


def test_triage_instructions_mention_tool():
    assert "triage_observations" in TRIAGE_INSTRUCTIONS


def test_triage_prompt_truncates_long_content():
    obs = [{"source_kind": "telegram", "actors": [], "content_text": "x" * 1000}]
    prompt = build_triage_prompt(obs)
    assert len(prompt) < 1500
```

- [ ] **Step 4: Run prompt tests**

Run: `cd ~/dev/ai/pulse/scripts && python -m pytest tests/test_extract_prompt.py tests/test_triage_prompt.py -v`

Expected: 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/dev/ai/pulse
git add scripts/extract/prompts.py scripts/tests/test_extract_prompt.py scripts/tests/test_triage_prompt.py
git commit -m "refactor: simplify prompts for tool-use, remove text parsers"
```

---

### Task 4: Tool-Use API Conversion

**Files:**
- Modify: `scripts/pulse_extract.py` (lines 1-72: imports + call functions)

- [ ] **Step 1: Write a test for tool-use triage call signature**

Add to `scripts/tests/test_extract_phase1.py`:

```python
def test_call_sonnet_triage_returns_tuple(tmp_path, monkeypatch):
    """call_sonnet_triage returns (verdicts, usage_info) tuple."""
    import pulse_extract

    class FakeBlock:
        type = "tool_use"
        name = "triage_observations"
        input = {"verdicts": [{"index": 1, "verdict": "extract", "reason": "has people"}]}

    class FakeUsage:
        input_tokens = 100
        output_tokens = 50

    class FakeMsg:
        content = [FakeBlock()]
        usage = FakeUsage()

    monkeypatch.setattr(pulse_extract, "_anthropic_client", lambda: type("C", (), {
        "messages": type("M", (), {"create": staticmethod(lambda **kw: FakeMsg())})()
    })())
    verdicts, usage = pulse_extract.call_sonnet_triage("test prompt", 1)
    assert len(verdicts) == 1
    assert verdicts[0]["verdict"] == "extract"
    assert usage["input_tokens"] == 100
    assert usage["model"] == pulse_extract.TRIAGE_MODEL


def test_call_opus_extract_returns_tuple(tmp_path, monkeypatch):
    """call_opus_extract returns (extraction_dict, usage_info) tuple."""
    import pulse_extract

    class FakeBlock:
        type = "tool_use"
        name = "save_extraction"
        input = {"entities": [{"canonical_name": "Anna", "kind": "person"}], "relations": [], "events": [], "facts": [], "merge_candidates": []}

    class FakeUsage:
        input_tokens = 500
        output_tokens = 200

    class FakeMsg:
        content = [FakeBlock()]
        usage = FakeUsage()

    monkeypatch.setattr(pulse_extract, "_anthropic_client", lambda: type("C", (), {
        "messages": type("M", (), {"create": staticmethod(lambda **kw: FakeMsg())})()
    })())
    data, usage = pulse_extract.call_opus_extract("test prompt")
    assert data["entities"][0]["canonical_name"] == "Anna"
    assert usage["output_tokens"] == 200
    assert usage["model"] == pulse_extract.EXTRACT_MODEL
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/dev/ai/pulse/scripts && python -m pytest tests/test_extract_phase1.py -v -k "call_sonnet_triage_returns_tuple or call_opus_extract_returns_tuple"`

Expected: FAIL (current functions return single value, not tuple).

- [ ] **Step 3: Update imports and call functions in pulse_extract.py**

In `scripts/pulse_extract.py`, add import after existing imports (line ~15):

```python
from extract import tool_schemas
```

Replace `call_sonnet_triage` (lines 53-61) with:

```python
def call_sonnet_triage(prompt: str, expected_count: int) -> tuple[list[dict], dict]:
    client = _anthropic_client()
    msg = client.messages.create(
        model=TRIAGE_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
        tools=[tool_schemas.TRIAGE_TOOL],
        tool_choice={"type": "tool", "name": "triage_observations"},
    )
    usage = {"input_tokens": msg.usage.input_tokens, "output_tokens": msg.usage.output_tokens, "model": TRIAGE_MODEL}
    for block in msg.content:
        if block.type == "tool_use" and block.name == "triage_observations":
            return block.input["verdicts"], usage
    raise ValueError("Sonnet did not call triage_observations tool")
```

Replace `call_opus_extract` (lines 64-72) with:

```python
def call_opus_extract(prompt: str) -> tuple[dict, dict]:
    client = _anthropic_client()
    msg = client.messages.create(
        model=EXTRACT_MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
        tools=[tool_schemas.EXTRACT_TOOL],
        tool_choice={"type": "tool", "name": "save_extraction"},
    )
    usage = {"input_tokens": msg.usage.input_tokens, "output_tokens": msg.usage.output_tokens, "model": EXTRACT_MODEL}
    for block in msg.content:
        if block.type == "tool_use" and block.name == "save_extraction":
            return block.input, usage
    raise ValueError("Opus did not call save_extraction tool")
```

Also remove `from extract import prompts` usage of `prompts.parse_triage_response` and `prompts.parse_extract_response` — these are no longer called. The import of `prompts` itself stays (used in `build_triage_prompt` and `build_extract_prompt` calls in `run_once`).

- [ ] **Step 4: Run new tests to verify they pass**

Run: `cd ~/dev/ai/pulse/scripts && python -m pytest tests/test_extract_phase1.py -v -k "call_sonnet_triage_returns_tuple or call_opus_extract_returns_tuple"`

Expected: 2 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/dev/ai/pulse
git add scripts/pulse_extract.py
git commit -m "feat: switch triage + extract to Anthropic tool-use API"
```

---

### Task 5: Update run_once + _apply_extraction for New Fields

**Files:**
- Modify: `scripts/pulse_extract.py` (run_once: unpack tuples + save metrics; _apply_extraction: provenance + relation context)

- [ ] **Step 1: Write test for extraction_metrics saving**

Add to `scripts/tests/test_extract_phase1.py`:

```python
def test_extraction_metrics_saved_after_fresh_call(tmp_path, monkeypatch):
    """run_once saves extraction_metrics for fresh (non-cached) LLM calls."""
    import pulse_extract

    con = _fresh_db(tmp_path)
    _seed_job_with_obs(con, job_id=1, obs_ids=[1])

    mock_usage = {"input_tokens": 500, "output_tokens": 200, "model": "test-model"}
    monkeypatch.setattr(pulse_extract, "call_sonnet_triage",
        lambda prompt, count: ([{"verdict": "extract", "reason": "test"}], mock_usage))
    monkeypatch.setattr(pulse_extract, "call_opus_extract",
        lambda prompt: ({"entities": [], "relations": [], "events": [], "facts": [], "merge_candidates": []}, mock_usage))

    pulse_extract.run_once(str(tmp_path / "test.db"))

    metrics = con.execute("SELECT model, input_tokens, output_tokens FROM extraction_metrics").fetchall()
    assert len(metrics) == 2  # one for triage, one for extract
```

Where `_seed_job_with_obs` is a helper:

```python
def _seed_job_with_obs(con, job_id, obs_ids):
    """Insert observations and a pending extraction job."""
    import json
    now = "2026-04-16T00:00:00Z"
    for oid in obs_ids:
        con.execute(
            "INSERT OR IGNORE INTO observations (id, source_kind, source_id, content_text, actors, metadata, content_hash, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (oid, "telegram", f"tg_{oid}", f"Message {oid}", "[]", "{}", f"hash_{oid}", now),
        )
    con.execute(
        "INSERT INTO extraction_jobs (id, observation_ids, state, attempts, created_at) VALUES (?,?,?,?,?)",
        (job_id, json.dumps(obs_ids), "pending", 0, now),
    )
```

- [ ] **Step 2: Write test for relation context in _apply_extraction**

Add to `scripts/tests/test_extract_phase1.py`:

```python
def test_apply_extraction_writes_relation_context(tmp_path):
    con = _fresh_db(tmp_path)
    now = "2026-04-16T00:00:00Z"
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (1,'Anna','person',?,?)", (now, now))
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (2,'Nik','person',?,?)", (now, now))

    result = {
        "entities": [
            {"canonical_name": "Anna", "kind": "person", "aliases": ["Аня"]},
            {"canonical_name": "Nik", "kind": "person"},
        ],
        "relations": [{"from": "Anna", "to": "Nik", "kind": "spouse", "context": "married since 2020", "strength": 1.0}],
        "events": [],
        "facts": [],
        "merge_candidates": [],
    }
    import pulse_extract
    report = pulse_extract._apply_extraction(con, 1, result)
    row = con.execute("SELECT context FROM relations WHERE kind='spouse'").fetchone()
    assert row[0] == "married since 2020"


def test_apply_extraction_writes_fact_provenance(tmp_path):
    con = _fresh_db(tmp_path)
    now = "2026-04-16T00:00:00Z"
    con.execute("INSERT INTO observations (id, source_kind, source_id, content_text, actors, metadata, content_hash, created_at) VALUES (42,'tg','1','text','[]','{}','h',?)", (now,))
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (1,'Anna','person',?,?)", (now, now))

    result = {
        "entities": [{"canonical_name": "Anna", "kind": "person"}],
        "relations": [],
        "events": [],
        "facts": [{"entity": "Anna", "text": "Loves cats", "confidence": 0.9}],
        "merge_candidates": [],
    }
    import pulse_extract
    report = pulse_extract._apply_extraction(con, 42, result)
    row = con.execute("SELECT source_obs_id, extractor_version FROM facts WHERE text='Loves cats'").fetchone()
    assert row[0] == 42
    assert row[1] == "v2"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd ~/dev/ai/pulse/scripts && python -m pytest tests/test_extract_phase1.py -v -k "relation_context or fact_provenance or metrics_saved"`

Expected: FAIL.

- [ ] **Step 4: Update _apply_extraction**

In `scripts/pulse_extract.py`, modify the relations section (around line 221) to include `context`:

Replace the relations INSERT (line 221-228):
```python
            cur = con.execute(
                """INSERT INTO relations (from_entity_id, to_entity_id, kind, strength, first_seen, last_seen, context)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(from_entity_id, to_entity_id, kind) DO UPDATE SET
                       strength  = strength + 1,
                       last_seen = excluded.last_seen,
                       context   = COALESCE(excluded.context, context)""",
                (from_id, to_id, rel.get("kind", ""), float(rel.get("strength", 0.0)), now, now, rel.get("context")),
            )
```

Modify the facts INSERT (around line 251-255) to include `source_obs_id` and `extractor_version`:

```python
            cur = con.execute(
                """INSERT INTO facts (entity_id, text, confidence, scorer_version, source_obs_id, extractor_version, created_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(entity_id, text) DO NOTHING""",
                (entity_id, fact.get("text", ""), scored["confidence"], scored["scorer_version"], obs_id, "v2", now),
            )
```

Modify the entities INSERT (around line 135-139) to include `extractor_version`:

```python
                cur = con.execute(
                    "INSERT INTO entities (canonical_name, kind, aliases, first_seen, last_seen, salience_score, emotional_weight, scorer_version, extractor_version) VALUES (?,?,?,?,?,?,?,?,?)",
                    (ent["canonical_name"], ent.get("kind", "person"), json.dumps(ent.get("aliases") or []),
                     now, now, scored["salience_score"], scored["emotional_weight"], scored["scorer_version"], "v2"),
                )
```

- [ ] **Step 5: Add _save_metrics helper and update run_once**

Add helper function in `scripts/pulse_extract.py` (after `_save_artifact`, around line 334):

```python
def _save_metrics(con: sqlite3.Connection, job_id: int, usage: dict) -> None:
    con.execute(
        "INSERT INTO extraction_metrics (job_id, model, input_tokens, output_tokens) VALUES (?,?,?,?)",
        (job_id, usage.get("model", "unknown"), usage.get("input_tokens"), usage.get("output_tokens")),
    )
```

Update `run_once` to unpack tuples and save metrics. In the triage section (around line 366):

```python
                    triage_prompt = prompts.build_triage_prompt(observations)
                    verdicts, triage_usage = call_sonnet_triage(triage_prompt, expected_count=len(observations))
                    _save_artifact(con, job_id, "triage", None, verdicts, TRIAGE_MODEL)
                    _save_metrics(con, job_id, triage_usage)
```

In the extract section (around line 381):

```python
                        result, extract_usage = call_opus_extract(extract_prompt)
                        _save_artifact(con, job_id, "extract", obs["id"], result, EXTRACT_MODEL)
                        _save_metrics(con, job_id, extract_usage)
```

- [ ] **Step 6: Run tests**

Run: `cd ~/dev/ai/pulse/scripts && python -m pytest tests/test_extract_phase1.py -v -k "relation_context or fact_provenance or metrics_saved"`

Expected: 3 tests PASS.

- [ ] **Step 7: Commit**

```bash
cd ~/dev/ai/pulse
git add scripts/pulse_extract.py
git commit -m "feat: add relation context, fact provenance, extraction metrics to apply/run_once"
```

---

### Task 6: Update All Test Mocks for Tuple Returns

**Files:**
- Modify: `scripts/tests/test_extract_phase1.py`
- Modify: `scripts/tests/test_extract_e2e.py`
- Modify: `scripts/tests/test_extract_loop.py`

- [ ] **Step 1: Define MOCK_USAGE constant in test files**

Add near the top of each test file that mocks `call_sonnet_triage` or `call_opus_extract`:

```python
MOCK_USAGE = {"input_tokens": 0, "output_tokens": 0, "model": "test"}
```

- [ ] **Step 2: Update all monkeypatch.setattr for call_sonnet_triage**

In every test file, find lines like:

```python
monkeypatch.setattr(pulse_extract, "call_sonnet_triage", lambda prompt, count: [...])
```

Change to:

```python
monkeypatch.setattr(pulse_extract, "call_sonnet_triage", lambda prompt, count: ([...], MOCK_USAGE))
```

The value inside `[...]` stays the same — wrap it in a tuple with `MOCK_USAGE`.

- [ ] **Step 3: Update all monkeypatch.setattr for call_opus_extract**

Find lines like:

```python
monkeypatch.setattr(pulse_extract, "call_opus_extract", lambda prompt: {...})
```

Change to:

```python
monkeypatch.setattr(pulse_extract, "call_opus_extract", lambda prompt: ({...}, MOCK_USAGE))
```

- [ ] **Step 4: Update test_extract_e2e.py fixture loading**

In `scripts/tests/test_extract_e2e.py`, the fixture loads `extract_responses.json`. The mock for `call_opus_extract` uses the fixture dict. Update:

```python
monkeypatch.setattr(pulse_extract, "call_opus_extract", lambda prompt: (fixtures["extract_1"], MOCK_USAGE))
```

For `call_sonnet_triage`, the fixture currently returns a text string that gets parsed. With tool-use, it should return structured verdicts directly:

```python
monkeypatch.setattr(pulse_extract, "call_sonnet_triage", lambda prompt, count: (
    [{"verdict": "extract", "reason": "mentions family"}, {"verdict": "skip", "reason": "trivial greeting"}],
    MOCK_USAGE,
))
```

- [ ] **Step 5: Run full test suite**

Run: `cd ~/dev/ai/pulse/scripts && python -m pytest tests/ -v`

Expected: ALL tests PASS. This is the critical validation that tool-use conversion doesn't break anything.

- [ ] **Step 6: Commit**

```bash
cd ~/dev/ai/pulse
git add scripts/tests/
git commit -m "test: update all mocks for tool-use tuple returns (data, usage)"
```

---

### Task 7: Keyword Retrieval

**Files:**
- Create: `scripts/extract/retrieval.py`
- Create: `scripts/tests/test_retrieval.py`

- [ ] **Step 1: Write retrieval tests**

Create `scripts/tests/test_retrieval.py`:

```python
"""Tests for keyword-based graph retrieval."""

import json
import sqlite3
from pathlib import Path

import pytest


def _fresh_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    migrations_dir = Path(__file__).resolve().parent.parent.parent / "internal" / "store" / "migrations"
    for sql_file in sorted(migrations_dir.glob("*.sql")):
        con.executescript(sql_file.read_text())
    return con


def _seed_graph(con):
    """Seed a small graph: Anna (person), Nik (person), Pulse (project), with relations and facts."""
    now = "2026-04-16T00:00:00Z"
    con.execute("INSERT INTO entities (id, canonical_name, kind, aliases, first_seen, last_seen, salience_score) VALUES (1,'Anna','person',?,?,?,0.9)", (json.dumps(["Аня", "Анна"]), now, now))
    con.execute("INSERT INTO entities (id, canonical_name, kind, aliases, first_seen, last_seen, salience_score) VALUES (2,'Nik','person',?,?,?,1.0)", (json.dumps(["Никита"]), now, now))
    con.execute("INSERT INTO entities (id, canonical_name, kind, aliases, first_seen, last_seen, salience_score) VALUES (3,'Pulse','project',?,?,?,0.8)", (json.dumps(["pulse-engine"]), now, now))
    con.execute("INSERT INTO relations (from_entity_id, to_entity_id, kind, strength, context, first_seen, last_seen) VALUES (1,2,'spouse',1.0,'married since 2020',?,?)", (now, now))
    con.execute("INSERT INTO relations (from_entity_id, to_entity_id, kind, strength, first_seen, last_seen) VALUES (2,3,'creator',1.0,?,?)", (now, now))
    con.execute("INSERT INTO facts (entity_id, text, confidence, created_at) VALUES (1,'Loves cats',0.9,?)", (now,))
    con.execute("INSERT INTO facts (entity_id, text, confidence, created_at) VALUES (3,'Written in Go and Python',0.95,?)", (now,))


def test_retrieve_by_canonical_name(tmp_path):
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    _seed_graph(con)
    result = retrieve_context(con, "Давай обсудим Anna")
    assert result["total_matched"] >= 1
    names = [e["canonical_name"] for e in result["matched_entities"]]
    assert "Anna" in names


def test_retrieve_by_alias(tmp_path):
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    _seed_graph(con)
    result = retrieve_context(con, "Аня сегодня устала")
    names = [e["canonical_name"] for e in result["matched_entities"]]
    assert "Anna" in names


def test_retrieve_includes_relations(tmp_path):
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    _seed_graph(con)
    result = retrieve_context(con, "Anna")
    anna = [e for e in result["matched_entities"] if e["canonical_name"] == "Anna"][0]
    rel_kinds = [r["kind"] for r in anna["relations"]]
    assert "spouse" in rel_kinds


def test_retrieve_includes_facts(tmp_path):
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    _seed_graph(con)
    result = retrieve_context(con, "Anna")
    anna = [e for e in result["matched_entities"] if e["canonical_name"] == "Anna"][0]
    fact_texts = [f["text"] for f in anna["facts"]]
    assert "Loves cats" in fact_texts


def test_retrieve_respects_top_k(tmp_path):
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    _seed_graph(con)
    result = retrieve_context(con, "Anna Nik Pulse", top_k=2)
    assert len(result["matched_entities"]) <= 2


def test_retrieve_no_match_returns_empty(tmp_path):
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    _seed_graph(con)
    result = retrieve_context(con, "XYZNONEXISTENT")
    assert result["total_matched"] == 0
    assert result["matched_entities"] == []


def test_retrieve_method_is_keyword(tmp_path):
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    _seed_graph(con)
    result = retrieve_context(con, "Anna")
    assert result["retrieval_method"] == "keyword"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/dev/ai/pulse/scripts && python -m pytest tests/test_retrieval.py -v`

Expected: FAIL (retrieval.py doesn't exist yet).

- [ ] **Step 3: Implement retrieval.py**

Create `scripts/extract/retrieval.py`:

```python
"""Keyword-based graph retrieval for Phase 2.

Tokenize user message → match entities by name/alias → 1-hop expansion → rank.
"""

import json
import re
import sqlite3
from datetime import datetime, timezone


def retrieve_context(
    con: sqlite3.Connection, message: str, top_k: int = 10
) -> dict:
    tokens = _tokenize(message)
    matched = _match_entities(con, tokens)

    for ent in matched:
        ent["relations"] = _get_relations(con, ent["id"])
        ent["facts"] = _get_facts(con, ent["id"])

    ranked = _rank(matched)
    trimmed = ranked[:top_k]

    return {
        "matched_entities": trimmed,
        "total_matched": len(matched),
        "retrieval_method": "keyword",
    }


def _tokenize(message: str) -> list[str]:
    words = re.findall(r"\b[\w\-]{2,}\b", message, re.UNICODE)
    ngrams = []
    for i in range(len(words)):
        for j in range(i + 1, min(i + 4, len(words) + 1)):
            ngrams.append(" ".join(words[i:j]))
    return list(set(words + ngrams))


def _match_entities(con: sqlite3.Connection, tokens: list[str]) -> list[dict]:
    matched: dict[int, dict] = {}
    all_entities = con.execute(
        "SELECT id, canonical_name, kind, aliases, salience_score, last_seen FROM entities"
    ).fetchall()

    for row in all_entities:
        eid, name, kind, aliases_json, salience, last_seen = row
        aliases = json.loads(aliases_json) if aliases_json else []
        all_names = [name] + aliases

        for token in tokens:
            if any(token.lower() == n.lower() for n in all_names):
                if eid not in matched:
                    matched[eid] = {
                        "id": eid,
                        "canonical_name": name,
                        "kind": kind,
                        "aliases": aliases,
                        "salience_score": salience,
                        "last_seen": last_seen,
                    }
                break

    return list(matched.values())


def _get_relations(con: sqlite3.Connection, entity_id: int) -> list[dict]:
    rows = con.execute(
        "SELECT r.kind, r.context, r.strength, "
        "CASE WHEN r.from_entity_id = ? THEN e2.canonical_name ELSE e1.canonical_name END "
        "FROM relations r "
        "JOIN entities e1 ON r.from_entity_id = e1.id "
        "JOIN entities e2 ON r.to_entity_id = e2.id "
        "WHERE r.from_entity_id = ? OR r.to_entity_id = ?",
        (entity_id, entity_id, entity_id),
    ).fetchall()
    return [
        {"kind": r[0], "context": r[1] or "", "strength": r[2], "other_entity": r[3]}
        for r in rows
    ]


def _get_facts(con: sqlite3.Connection, entity_id: int) -> list[dict]:
    rows = con.execute(
        "SELECT text, confidence, verified FROM facts WHERE entity_id = ?",
        (entity_id,),
    ).fetchall()
    return [{"text": r[0], "confidence": r[1], "verified": bool(r[2])} for r in rows]


def _rank(entities: list[dict]) -> list[dict]:
    now = datetime.now(timezone.utc)
    scored = []
    for ent in entities:
        try:
            last = datetime.fromisoformat(ent["last_seen"].replace("Z", "+00:00"))
            days_ago = (now - last).days
        except (ValueError, AttributeError):
            days_ago = 365
        recency = max(0.1, 1.0 - days_ago / 365)
        scored.append((ent["salience_score"] * recency, ent))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [ent for _, ent in scored]
```

- [ ] **Step 4: Run retrieval tests**

Run: `cd ~/dev/ai/pulse/scripts && python -m pytest tests/test_retrieval.py -v`

Expected: 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/dev/ai/pulse
git add scripts/extract/retrieval.py scripts/tests/test_retrieval.py
git commit -m "feat: keyword-based graph retrieval with 1-hop expansion"
```

---

### Task 8: Consolidation Pipeline

**Files:**
- Create: `scripts/pulse_consolidate.py`
- Create: `scripts/tests/test_consolidate.py`

- [ ] **Step 1: Write consolidation tests**

Create `scripts/tests/test_consolidate.py`:

```python
"""Tests for Pulse graph consolidation."""

import json
import sqlite3
import time
from pathlib import Path

import pytest


def _fresh_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    migrations_dir = Path(__file__).resolve().parent.parent.parent / "internal" / "store" / "migrations"
    for sql_file in sorted(migrations_dir.glob("*.sql")):
        con.executescript(sql_file.read_text())
    return con, str(tmp_path / "test.db")


def test_find_duplicate_candidates_detects_similar_names(tmp_path):
    from pulse_consolidate import find_duplicate_candidates
    con, _ = _fresh_db(tmp_path)
    now = "2026-04-16T00:00:00Z"
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (1,'Anna','person',?,?)", (now, now))
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (2,'Анна','person',?,?)", (now, now))
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (3,'Pulse','project',?,?)", (now, now))

    dupes = find_duplicate_candidates(con)
    assert len(dupes) == 0  # "Anna" vs "Анна" — different scripts, SequenceMatcher < 0.8


def test_find_duplicate_candidates_catches_close_names(tmp_path):
    from pulse_consolidate import find_duplicate_candidates
    con, _ = _fresh_db(tmp_path)
    now = "2026-04-16T00:00:00Z"
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (1,'Alexander','person',?,?)", (now, now))
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (2,'Aleksander','person',?,?)", (now, now))

    dupes = find_duplicate_candidates(con)
    assert len(dupes) == 1
    assert dupes[0]["similarity"] >= 0.8


def test_find_duplicates_ignores_different_kinds(tmp_path):
    from pulse_consolidate import find_duplicate_candidates
    con, _ = _fresh_db(tmp_path)
    now = "2026-04-16T00:00:00Z"
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (1,'Pulse','project',?,?)", (now, now))
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (2,'Pulse','product',?,?)", (now, now))

    dupes = find_duplicate_candidates(con)
    assert len(dupes) == 0


def test_close_stale_questions(tmp_path):
    from pulse_consolidate import close_stale_questions
    con, _ = _fresh_db(tmp_path)
    now = "2026-04-16T00:00:00Z"
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (1,'X','person',?,?)", (now, now))
    past = "2020-01-01T00:00:00Z"
    future = "2099-01-01T00:00:00Z"
    con.execute("INSERT INTO open_questions (subject_entity_id, question_text, asked_at, ttl_expires_at, state) VALUES (1,'stale?',?,?,'open')", (now, past))
    con.execute("INSERT INTO open_questions (subject_entity_id, question_text, asked_at, ttl_expires_at, state) VALUES (1,'fresh?',?,?,'open')", (now, future))

    closed = close_stale_questions(con)
    assert closed == 1
    states = [r[0] for r in con.execute("SELECT state FROM open_questions ORDER BY id").fetchall()]
    assert states == ["auto_closed", "open"]


def test_entity_stats(tmp_path):
    from pulse_consolidate import entity_stats
    con, _ = _fresh_db(tmp_path)
    now = "2026-04-16T00:00:00Z"
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (1,'A','person',?,?)", (now, now))
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (2,'B','person',?,?)", (now, now))
    con.execute("INSERT INTO entities (id, canonical_name, kind, first_seen, last_seen) VALUES (3,'C','project',?,?)", (now, now))
    con.execute("INSERT INTO relations (from_entity_id, to_entity_id, kind, strength, first_seen, last_seen) VALUES (1,2,'friend',1.0,?,?)", (now, now))
    con.execute("INSERT INTO facts (entity_id, text, confidence, created_at) VALUES (1,'fact 1',0.9,?)", (now,))

    stats = entity_stats(con)
    assert stats["total_entities"] == 3
    assert stats["entities_by_kind"]["person"] == 2
    assert stats["entities_by_kind"]["project"] == 1
    assert stats["orphan_entities"] == 1  # entity 3 has no relations or facts
    assert stats["total_relations"] == 1
    assert stats["total_facts"] == 1


def test_process_approved_merges(tmp_path):
    from pulse_consolidate import process_approved_merges
    con, _ = _fresh_db(tmp_path)
    now = "2026-04-16T00:00:00Z"
    con.execute("INSERT INTO entities (id, canonical_name, kind, aliases, first_seen, last_seen) VALUES (1,'Anna','person',?,?,?)", (json.dumps(["Аня"]), now, now))
    con.execute("INSERT INTO entities (id, canonical_name, kind, aliases, first_seen, last_seen) VALUES (2,'Анна','person','[]',?,?)", (now, now))
    con.execute("INSERT INTO facts (entity_id, text, confidence, created_at) VALUES (2,'Fact about Анна',0.8,?)", (now,))
    con.execute("INSERT INTO entity_merge_proposals (from_entity_id, to_entity_id, confidence, evidence_md, state, proposed_at) VALUES (2,1,0.95,'similar names','approved',?)", (now,))

    merged = process_approved_merges(con)
    assert merged == 1

    # from_entity (id=2) should be deleted
    assert con.execute("SELECT COUNT(*) FROM entities WHERE id=2").fetchone()[0] == 0
    # fact repointed to entity 1
    assert con.execute("SELECT entity_id FROM facts WHERE text='Fact about Анна'").fetchone()[0] == 1
    # aliases merged
    aliases = json.loads(con.execute("SELECT aliases FROM entities WHERE id=1").fetchone()[0])
    assert "Анна" in aliases


def test_run_consolidation_end_to_end(tmp_path):
    from pulse_consolidate import run_consolidation
    _, db_path = _fresh_db(tmp_path)
    report = run_consolidation(db_path)
    assert "stats" in report
    assert "duplicate_candidates" in report
    assert "stale_questions_closed" in report
    assert "merges_executed" in report
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/dev/ai/pulse/scripts && python -m pytest tests/test_consolidate.py -v`

Expected: FAIL (pulse_consolidate.py doesn't exist yet).

- [ ] **Step 3: Implement pulse_consolidate.py**

Create `scripts/pulse_consolidate.py`:

```python
#!/usr/bin/env python3
"""Pulse graph consolidation: dedup entities, close stale questions, execute merges, report stats."""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from difflib import SequenceMatcher


def _open_connection(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, isolation_level=None)
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 5000")
    return con


def find_duplicate_candidates(
    con: sqlite3.Connection, threshold: float = 0.8
) -> list[dict]:
    entities = con.execute(
        "SELECT id, canonical_name, kind FROM entities"
    ).fetchall()

    candidates = []
    for i in range(len(entities)):
        for j in range(i + 1, len(entities)):
            e1, e2 = entities[i], entities[j]
            if e1[2] != e2[2]:
                continue
            sim = SequenceMatcher(None, e1[1].lower(), e2[1].lower()).ratio()
            if sim >= threshold:
                candidates.append({
                    "entity_a_id": e1[0],
                    "entity_a_name": e1[1],
                    "entity_b_id": e2[0],
                    "entity_b_name": e2[1],
                    "kind": e1[2],
                    "similarity": round(sim, 3),
                })
    return candidates


def close_stale_questions(con: sqlite3.Connection) -> int:
    now = datetime.now(timezone.utc).isoformat()
    result = con.execute(
        "UPDATE open_questions SET state = 'auto_closed' "
        "WHERE state = 'open' AND ttl_expires_at < ?",
        (now,),
    )
    return result.rowcount


def entity_stats(con: sqlite3.Connection) -> dict:
    by_kind: dict[str, int] = {}
    for row in con.execute("SELECT kind, COUNT(*) FROM entities GROUP BY kind"):
        by_kind[row[0]] = row[1]

    orphans = con.execute(
        "SELECT COUNT(*) FROM entities e "
        "WHERE NOT EXISTS (SELECT 1 FROM relations r WHERE r.from_entity_id = e.id OR r.to_entity_id = e.id) "
        "AND NOT EXISTS (SELECT 1 FROM facts f WHERE f.entity_id = e.id)"
    ).fetchone()[0]

    return {
        "entities_by_kind": by_kind,
        "total_entities": sum(by_kind.values()) if by_kind else 0,
        "orphan_entities": orphans,
        "total_relations": con.execute("SELECT COUNT(*) FROM relations").fetchone()[0],
        "total_facts": con.execute("SELECT COUNT(*) FROM facts").fetchone()[0],
    }


def process_approved_merges(con: sqlite3.Connection) -> int:
    approved = con.execute(
        "SELECT id, from_entity_id, to_entity_id FROM entity_merge_proposals "
        "WHERE state = 'approved'"
    ).fetchall()

    merged = 0
    for proposal_id, from_id, to_id in approved:
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute("UPDATE relations SET from_entity_id = ? WHERE from_entity_id = ?", (to_id, from_id))
            con.execute("UPDATE relations SET to_entity_id = ? WHERE to_entity_id = ?", (to_id, from_id))
            con.execute("UPDATE facts SET entity_id = ? WHERE entity_id = ?", (to_id, from_id))
            con.execute(
                "UPDATE evidence SET subject_id = ? WHERE subject_kind = 'entity' AND subject_id = ?",
                (to_id, from_id),
            )
            con.execute("UPDATE OR IGNORE event_entities SET entity_id = ? WHERE entity_id = ?", (to_id, from_id))

            from_row = con.execute("SELECT canonical_name, aliases FROM entities WHERE id = ?", (from_id,)).fetchone()
            to_row = con.execute("SELECT aliases FROM entities WHERE id = ?", (to_id,)).fetchone()

            merged_aliases: set[str] = set()
            if from_row and from_row[1]:
                merged_aliases.update(json.loads(from_row[1]))
            if to_row and to_row[0]:
                merged_aliases.update(json.loads(to_row[0]))
            if from_row:
                merged_aliases.add(from_row[0])

            con.execute("UPDATE entities SET aliases = ? WHERE id = ?", (json.dumps(sorted(merged_aliases)), to_id))
            con.execute(
                "UPDATE entities SET salience_score = MAX(salience_score, "
                "(SELECT salience_score FROM entities WHERE id = ?)) WHERE id = ?",
                (from_id, to_id),
            )

            con.execute("DELETE FROM entities WHERE id = ?", (from_id,))
            con.execute("UPDATE entity_merge_proposals SET state = 'auto_merged' WHERE id = ?", (proposal_id,))
            con.execute("COMMIT")
            merged += 1
        except Exception:
            con.execute("ROLLBACK")
            raise

    return merged


def run_consolidation(db_path: str) -> dict:
    con = _open_connection(db_path)
    stats = entity_stats(con)
    duplicates = find_duplicate_candidates(con)
    closed = close_stale_questions(con)
    merges = process_approved_merges(con)
    con.close()

    return {
        "stats": stats,
        "duplicate_candidates": len(duplicates),
        "duplicates": duplicates[:20],
        "stale_questions_closed": closed,
        "merges_executed": merges,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Pulse graph consolidation")
    parser.add_argument("--db", required=True, help="Path to pulse database")
    args = parser.parse_args()

    report = run_consolidation(args.db)

    print("=== Consolidation Report ===")
    print(f"Entities: {report['stats']['total_entities']} ({report['stats']['entities_by_kind']})")
    print(f"Orphans: {report['stats']['orphan_entities']}")
    print(f"Relations: {report['stats']['total_relations']}")
    print(f"Facts: {report['stats']['total_facts']}")
    print(f"Duplicate candidates: {report['duplicate_candidates']}")
    for dup in report["duplicates"]:
        print(f"  {dup['entity_a_name']} <-> {dup['entity_b_name']} ({dup['similarity']})")
    print(f"Stale questions closed: {report['stale_questions_closed']}")
    print(f"Merges executed: {report['merges_executed']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run consolidation tests**

Run: `cd ~/dev/ai/pulse/scripts && python -m pytest tests/test_consolidate.py -v`

Expected: 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/dev/ai/pulse
git add scripts/pulse_consolidate.py scripts/tests/test_consolidate.py
git commit -m "feat: consolidation pipeline — dedup, stale cleanup, merge execution"
```

---

### Task 9: Full Suite + Integration Smoke

**Files:**
- No new files — validation only

- [ ] **Step 1: Run full test suite**

Run: `cd ~/dev/ai/pulse/scripts && python -m pytest tests/ -v`

Expected: ALL tests pass. Count should be ~75+ (60 existing + 6 migration + 6 schema + 2 call + 3 apply + 7 retrieval + 7 consolidation + updated prompt/triage tests).

- [ ] **Step 2: Verify migration applies to real data**

```bash
cd ~/dev/ai/pulse
cp /Users/nikshilov/dev/ai/pulse/pulse-dev.db /tmp/pulse-phase2-test.db
python3 -c "
import sqlite3
con = sqlite3.connect('/tmp/pulse-phase2-test.db')
con.executescript(open('internal/store/migrations/007_phase_2.sql').read())
# Verify new columns exist
print('entity kinds:', [r[0] for r in con.execute('PRAGMA table_info(entities)').fetchall()])
print('fact cols:', [r[1] for r in con.execute('PRAGMA table_info(facts)').fetchall()])
print('metrics table:', con.execute('SELECT name FROM sqlite_master WHERE name=\"extraction_metrics\"').fetchone())
print('relation context col:', any(r[1] == 'context' for r in con.execute('PRAGMA table_info(relations)').fetchall()))
con.close()
print('Migration 007 applied successfully')
"
```

Expected: Migration applies without error. New columns and table visible.

- [ ] **Step 3: Run a single extraction job on test DB (dry run)**

```bash
cd ~/dev/ai/pulse/scripts
python3 -c "
import pulse_extract
# Just verify the module loads cleanly with new imports
print('TRIAGE_MODEL:', pulse_extract.TRIAGE_MODEL)
print('EXTRACT_MODEL:', pulse_extract.EXTRACT_MODEL)
from extract import tool_schemas
print('EXTRACT_TOOL name:', tool_schemas.EXTRACT_TOOL['name'])
print('TRIAGE_TOOL name:', tool_schemas.TRIAGE_TOOL['name'])
from extract import retrieval
print('retrieval module loaded')
import pulse_consolidate
print('consolidation module loaded')
print('All modules load cleanly')
"
```

Expected: All modules import without error.

- [ ] **Step 4: Commit any remaining fixes**

If any tests failed or imports broke, fix and commit.

- [ ] **Step 5: Final commit with branch summary**

```bash
cd ~/dev/ai/pulse
git log --oneline -10
```

Verify the commit history shows clean Phase 2 progression.

---

## Self-Review Checklist

### Spec Coverage

| Spec Section | Task |
|-------------|------|
| 2.1 Tool-Use Structured Output | Tasks 2, 3, 4 |
| 2.2 Rich Schema (Migration 007) | Task 1 |
| 2.3 Basic Retrieval | Task 7 |
| 2.4 Consolidation | Task 8 |
| 2.5 Success Criteria | Task 9 (validation) |
| Appendix A.1 Provenance | Task 5 (source_obs_id, extractor_version) |
| Appendix A.5 Observability | Task 5 (extraction_metrics) |
| Appendix A.6 Graceful Degradation | Task 4 (ValueError on missing tool call) |

### Not in This Plan (by design — Phase 3/4)

- Embedding pipeline (sqlite-vec, Phase 3)
- Semantic retrieval (Phase 3)
- Alias index normalization (Phase 3)
- Merge confirmation via Telegram (Phase 3)
- MCP graph server (Phase 4)
- Web viewer (Phase 4)
- Human-in-the-loop (Phase 4)
- Dynamic entity type discovery (Phase 4)
