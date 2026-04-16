# Phase 2b: Graph Intelligence — Design Spec

> **Origin:** Research audit of cognitive agent community ideas (openclaw-deus, idle reflection, concept space, hormones, life metrics). 5-agent parallel evaluation on 2026-04-16. This spec captures validated ideas worth implementing.

## Problem Statement

Phase 2 unblocked writes (tool-use, 100% success rate on smoke). The graph now fills with entities, relations, facts. But:

1. **Retrieval is shallow** — fixed 1-hop. "How does Anna feel about my projects?" returns nothing (Anna→Nik→Pulse requires 2 hops)
2. **Graph is dead between conversations** — no consolidation, no decay, no proactive gap detection
3. **No observability** — we don't track extraction efficiency or emotional trajectory over time
4. **open_questions table exists but is never populated** — Elle can't ask meaningful questions

## Research Evaluation Summary

| Concept | Source | Verdict | Action |
|---------|--------|---------|--------|
| Adaptive graph traversal depth | Cognitive Light Cone | **Годно** | Implement (Phase 2b) |
| Consolidation cron (co-occurrence + gaps + Sonnet) | Idle Reflection | **Годно** | Implement (Phase 2b) |
| Salience decay with exp(-λ×days) | openclaw-deus belief decay | **Годно** | Implement (Phase 2b) |
| open_questions auto-population | Idle Reflection | **Годно** | Implement (Phase 2b) |
| Valence trend (rolling sentiment avg) | Life Metrics | **Годно** | Implement (Phase 2b) |
| Extraction efficiency metric | Life Metrics | **Годно** | Implement (Phase 2b) |
| Consolidation skip-guard | Cognitive Light Cone | **Годно** | Implement (Phase 2b) |
| Episodic replay (re-score old episodes) | Idle Reflection | Интересно | Defer to Phase 3 |
| uncertainty_signal for retrieval breadth | Hormones (simplified) | Интересно | Defer to >500 entities |
| Spread activation / "Active Inference" | Cognitive Light Cone | **Не нужно** | Skip |
| Schema detection / meta-entities | Idle Reflection | **Не нужно** | Skip (premature) |
| Hormone system (DA/NE/Cortisol/5-HT) | Community screenshots | **Не нужно** | Cargo cult, skip |
| Concept Space architecture | Community screenshots | **Не нужно** | Rebranded embeddings, skip |
| SurrealDB migration | SurrealDB evaluation | **Не нужно** | Killer problem: Go+Python dual access |
| Multi-agent idle loop | Idle Reflection | **Не нужно** | One cron > 5 "agents" |

---

## Phase 2b Features

### 2b.1 Adaptive Depth Retrieval

**File:** `scripts/extract/retrieval.py` (extend existing)

**Problem:** Current `retrieve_context()` does fixed 1-hop — matches entity by name/alias, returns its relations and facts. Indirect relationships (Anna→Nik→Pulse) are invisible.

**Solution:** BFS with configurable depth and hop penalty in ranking.

```python
def retrieve_context(
    con: sqlite3.Connection, message: str, top_k: int = 10, depth: int = 1
) -> dict:
    tokens = _tokenize(message)
    seed_entities = _match_entities(con, tokens)
    
    # BFS expansion to depth
    all_entities = {}
    frontier = [(eid, 0) for eid in [e["id"] for e in seed_entities]]
    
    for eid, hop in frontier:
        if eid in all_entities or hop > depth:
            continue
        entity = _get_entity_full(con, eid)  # name, kind, aliases, salience, relations, facts
        entity["_hop"] = hop
        all_entities[eid] = entity
        
        if hop < depth:
            neighbors = con.execute(
                "SELECT CASE WHEN from_entity_id=? THEN to_entity_id ELSE from_entity_id END, strength "
                "FROM relations WHERE (from_entity_id=? OR to_entity_id=?) AND strength > 0.3 "
                "ORDER BY strength DESC LIMIT 5",
                (eid, eid, eid),
            ).fetchall()
            for nid, _ in neighbors:
                frontier.append((nid, hop + 1))
    
    ranked = _rank(list(all_entities.values()))  # existing rank + hop penalty
    return {
        "matched_entities": ranked[:top_k],
        "total_matched": len(all_entities),
        "retrieval_method": "keyword",
        "max_depth_used": max((e["_hop"] for e in all_entities.values()), default=0),
    }
```

**Ranking with hop penalty:** In `_rank()`, add: `score *= 0.7 ** entity["_hop"]` — entities further away rank lower.

**Caller decides depth:**
- Default: depth=1 (backwards-compatible)
- If retrieval returns 0 matches with depth=1, retry with depth=2 (auto-escalation)
- Emotional/complex messages: depth=2 from start

**Tests:** 3 new tests:
- `test_retrieve_2hop_indirect_relation` — Anna→Nik→Pulse found via depth=2
- `test_hop_penalty_ranking` — direct match ranks above 2-hop match
- `test_depth_0_returns_only_matched` — depth=0 returns entity without relations

### 2b.2 Consolidation Cron Enrichment

**File:** `scripts/pulse_consolidate.py` (extend existing)

Add three new functions to the existing consolidation pipeline:

#### A. Co-occurrence Detection

```sql
-- New view or inline query
SELECT ee1.entity_id AS a, ee2.entity_id AS b, COUNT(*) AS co_count
FROM event_entities ee1
JOIN event_entities ee2 ON ee1.event_id = ee2.event_id 
    AND ee1.entity_id < ee2.entity_id
GROUP BY ee1.entity_id, ee2.entity_id
HAVING co_count >= 3
```

If entity pair co-occurs 3+ times but has no explicit relation → log as `implicit_relation_candidate` in consolidation report.

#### B. Knowledge Gap Detection

```sql
SELECT e.id, e.canonical_name, e.kind,
       COUNT(DISTINCT ee.event_id) AS mention_count,
       COUNT(DISTINCT f.id) AS fact_count
FROM entities e
LEFT JOIN event_entities ee ON ee.entity_id = e.id
LEFT JOIN facts f ON f.entity_id = e.id
GROUP BY e.id
HAVING mention_count > 3 AND fact_count < 2
ORDER BY mention_count DESC
LIMIT 10
```

Entities mentioned often but with few facts = knowledge gaps. Output as candidates for `open_questions`.

#### C. Auto-populate open_questions

For each knowledge gap found:
```python
con.execute(
    "INSERT OR IGNORE INTO open_questions (subject_entity_id, question_text, asked_at, ttl_expires_at, state) "
    "VALUES (?, ?, ?, ?, 'open')",
    (entity_id, f"Что сейчас с {entity_name}?", now, ttl_30_days),
)
```

#### D. Skip-guard

At the top of `run_consolidation()`:
```python
new_entities = con.execute(
    "SELECT COUNT(*) FROM entities WHERE first_seen > ?", (last_consolidation_ts,)
).fetchone()[0]
new_evidence = con.execute(
    "SELECT COUNT(*) FROM evidence WHERE created_at > ?", (last_consolidation_ts,)
).fetchone()[0]
if new_entities == 0 and new_evidence < 3:
    return {"skipped": True, "reason": "no significant changes"}
```

Save `last_consolidation_ts` in a simple metadata table or file.

### 2b.3 Salience Decay

**File:** `scripts/pulse_consolidate.py` (add to consolidation run)

Entities that haven't been seen in a while should lose salience. Formula from openclaw-deus belief decay:

```python
import math

DECAY_RATES = {
    "person": 0.001,    # people decay very slowly (λ per day)
    "project": 0.005,   # projects decay faster
    "place": 0.003,
    "concept": 0.01,    # concepts decay fastest
    "default": 0.005,
}

def decay_salience(con: sqlite3.Connection) -> int:
    """Apply exponential decay to salience scores. Returns count updated."""
    entities = con.execute(
        "SELECT id, kind, salience_score, last_seen FROM entities WHERE salience_score > 0.05"
    ).fetchall()
    updated = 0
    now = datetime.now(timezone.utc)
    for eid, kind, salience, last_seen_str in entities:
        try:
            last = datetime.fromisoformat(last_seen_str.replace("Z", "+00:00"))
            days = (now - last).days
        except (ValueError, AttributeError):
            days = 30
        if days < 1:
            continue
        lam = DECAY_RATES.get(kind, DECAY_RATES["default"])
        new_salience = salience * math.exp(-lam * days)
        new_salience = max(0.05, new_salience)  # floor
        if abs(new_salience - salience) > 0.01:
            con.execute("UPDATE entities SET salience_score = ? WHERE id = ?", (round(new_salience, 4), eid))
            updated += 1
    return updated
```

Add to `run_consolidation()` output: `"salience_decayed": decay_count`.

### 2b.4 Observability Metrics

**File:** `scripts/pulse_consolidate.py` (add to `entity_stats`)

Two new metrics computed from existing tables:

#### A. Valence Trend

```python
def valence_trend(con: sqlite3.Connection, days: int = 30) -> dict:
    rows = con.execute(
        "SELECT DATE(ts) as day, AVG(sentiment) as avg_sent, COUNT(*) as n "
        "FROM events WHERE ts > DATE('now', ?) GROUP BY DATE(ts) ORDER BY day",
        (f"-{days} days",),
    ).fetchall()
    if not rows:
        return {"trend": "no_data", "days": days}
    sentiments = [r[1] for r in rows if r[1] is not None]
    if len(sentiments) < 2:
        return {"trend": "insufficient", "days": days, "points": len(sentiments)}
    recent_avg = sum(sentiments[-7:]) / len(sentiments[-7:]) if len(sentiments) >= 7 else sentiments[-1]
    overall_avg = sum(sentiments) / len(sentiments)
    return {
        "trend": "improving" if recent_avg > overall_avg + 0.1 else "declining" if recent_avg < overall_avg - 0.1 else "stable",
        "recent_7d_avg": round(recent_avg, 3),
        "overall_avg": round(overall_avg, 3),
        "days": days,
        "data_points": len(sentiments),
    }
```

#### B. Extraction Efficiency

```python
def extraction_efficiency(con: sqlite3.Connection) -> dict:
    row = con.execute(
        "SELECT COUNT(*) as jobs, SUM(input_tokens + output_tokens) as total_tokens "
        "FROM extraction_metrics"
    ).fetchone()
    entity_count = con.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    if not row or not row[1] or row[1] == 0:
        return {"entities_per_1k_tokens": 0, "total_jobs": 0}
    return {
        "entities_per_1k_tokens": round(entity_count / (row[1] / 1000), 2),
        "total_jobs": row[0],
        "total_tokens": row[1],
        "total_entities": entity_count,
    }
```

Add both to `run_consolidation()` report.

---

## Success Criteria (Phase 2b)

| Criterion | Metric |
|-----------|--------|
| Adaptive depth | 2-hop retrieval returns indirect relationships in tests |
| Co-occurrence | Consolidation detects entity pairs with 3+ co-occurrences |
| Knowledge gaps | open_questions populated for high-mention/low-fact entities |
| Salience decay | Entities unseen 30+ days have lower salience after consolidation |
| Skip-guard | Consolidation skips when nothing changed |
| Valence trend | Trend computed from events.sentiment |
| Extraction efficiency | entities/1k_tokens metric in consolidation report |
| Backwards-compatible | All existing 86 tests still pass |

---

## What's NOT in Phase 2b (and why)

| Feature | Why deferred |
|---------|-------------|
| Embedding pipeline (sqlite-vec) | Phase 3 — needs OpenAI API, separate concern |
| Spread activation | Premature — graph too small (<1K entities) |
| Schema detection / meta-entities | Complexity without payoff at current scale |
| Hormone system | Cargo cult — 4 EMA filters ≠ neuroscience |
| Multi-agent idle loop | One cron > 5 "agents" |
| SurrealDB | Go+Python dual access killer problem |
| Uncertainty signal | Useful at >500 entities, revisit then |
| Episodic replay (re-scoring) | Interesting but needs baseline patterns first |
| Autonomous research pipeline | Separate initiative — see project memory |

---

## Appendix: Research Sources

### A. openclaw-deus (basilisk-labs/openclaw-deus)
- **What it is:** JS context management framework (16k LOC, 218 tests), NOT cognitive architecture
- **Worth stealing:** Belief decay formula (`confidence * exp(-λ * days)` with class-dependent rates), review-first promotion, action cost model
- **Not worth stealing:** Philosophy layer (axioms, ontological status), markdown-as-database

### B. Idle Reflection Architecture (community screenshots)
- **Episodic Replay:** Real cognitive mechanism, implementable on SQL (replay failed episodes in new context)
- **Curiosity/VOI:** Correct idea (UCB formula), needs concrete utility function — deferred
- **"Active Inference":** Mislabeled spread activation (Collins & Loftus 1975). The algorithm is useful, the name is cargo cult
- **Schema Detection:** Co-occurrence counting is useful (implemented). Abstraction layer is premature
- **LLM Psychologist/Teacher:** Best part — rare targeted LLM calls with clear triggers

### C. Concept Space + Cognitive Light Cone
- **Concept Space:** Rebranding of embedding space. Knowledge graph strictly more powerful
- **Cognitive Light Cone:** Adaptive traversal depth is genuinely useful — **implemented as adaptive depth retrieval**
- **Cognitive triggers:** Time-based + skip-guard gives 80% benefit at 5% complexity

### D. Hormone Simulation (DA/NE/Cortisol/5-HT)
- **Neuroscience accuracy:** 3.5/10. DA without prediction error ≠ dopamine. Cortisol with instant dynamics ≠ cortisol
- **Engineering:** 4 EMA filters with biological labels. `uncertainty_signal` (1 float) gives same behavioral benefit
- **Practical takeaway:** One `uncertainty_signal` > four fake hormones

### E. Life Metrics (5 axes)
- **Useful:** Valence trend (events.sentiment), extraction efficiency (entities/tokens) — both on existing tables
- **Not applicable:** Agency metrics (no autonomy engine), vitality (agent has no body), prediction precision (no prediction loop)
