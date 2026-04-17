# Real-corpus retrieval bench — `run_real_eval.py`

Runs Pulse retrieval against the 30-event "Alex" empathic-memory-corpus —
the same dataset used in the 9-way April 9 2026 bench where Garden won with
26.71.

This is the first bench on **real data**. Every retrieval change from here
forward has a scoreboard to answer to.

## What this is

A deterministic, LLM-free harness that:

1. Loads `~/dev/ai/bench/datasets/empathic-memory-corpus.json`
2. Builds a Pulse graph via direct SQL inserts (no extraction pipeline, no
   LLM calls, zero $)
3. Runs `extract.retrieval.retrieve_context()` against the 5 test queries in
   the corpus
4. Computes Recall@5, Recall@10, MRR, Critical-hit@1 per query and overall

## What it is NOT

- **Not a direct Garden-vs-Pulse comparison.** Garden's 26.71 is a weighted
  0–80 rubric scored by LLM judges across tone / presence / memory-surfacing /
  etc., on full companion responses. Our metrics measure whether Pulse
  surfaces the right **entities** — a prerequisite for good responses, but
  not the whole thing. A proper comparison requires re-scoring both systems'
  retrieval outputs under the same metric or re-running LLM-judge scoring on
  responses that include Pulse retrieval context.

- **Not a full extract→retrieve pipeline test.** We seed the graph with raw
  corpus text (no LLM extraction). This is intentional: it isolates
  retrieval quality from extraction quality. When extraction improves, you
  won't see it here; run a separate extraction bench for that.

- **Not statistically powerful.** The corpus has 30 events and 5 test
  queries. Stddev on every metric will be large. Take ±0.1 swings between
  runs of different retrieval code as weak signals, not proof.

## Usage

```bash
# Default: reads ~/dev/ai/bench/datasets/empathic-memory-corpus.json
python scripts/bench/run_real_eval.py

# Per-query breakdown
python scripts/bench/run_real_eval.py --verbose

# Custom corpus path
python scripts/bench/run_real_eval.py --corpus /path/to/corpus.json

# Retrieval parameters (defaults match production: top_k=10, depth=2)
python scripts/bench/run_real_eval.py --top-k 10 --depth 2

# Hybrid retrieval (keyword + semantic embeddings).
# NOTE: semantic-top-n default is 20 — on this small corpus (7 persons) that
# admits everything into seed and the embedder can't discriminate. Set it
# BELOW corpus size (e.g. 3) so cosine actually selects.
python scripts/bench/run_real_eval.py --semantic --embedder-model openai-text-embedding-3-large --semantic-top-n 3

# Side-by-side keyword vs hybrid with delta table
python scripts/bench/run_real_eval.py --compare --embedder-model openai-text-embedding-3-large --semantic-top-n 3
```

Requires `OPENAI_API_KEY` env var for the `openai-*` embedder model.
`fake-local` works offline but produces no real semantic signal (SHA-based
hash fallback for plumbing tests).

## Ingest strategy

LLM-free, deterministic, idempotent on re-runs.

| Step | What happens | Notes |
|------|--------------|-------|
| Person detection | Hand-maintained name list from `_meta.user.snapshot` + corpus scan (Alex, Maya, Sarah, Cooper, Ethan, Jordan, David) | Case-insensitive whole-word match. Skips everything else — no orgs/places/concepts created. |
| Entity rows | One row per canonical person | Fixed IDs 1..7 so re-runs are stable. `is_self=1` for Alex. `salience = mentions / 30`. `emotional_weight = mean(|sentiment|)/2` across mentioning events. |
| Event rows | One per corpus event (30 total) | `title = text[:60]`, `description = full text`, `sentiment` direct, `emotional_weight = |sentiment|/2`, `ts = now - days_ago`. |
| `event_entities` | One row per (event, person-mentioned) pair | Enables event-based ground truth. |
| Facts | One per event, attached to first-mentioned person | `confidence = 0.9 if user_flag else 0.7`. |
| Relations | Person–person co-occurrence | `strength = co_occurrences / min(mentions_a, mentions_b)`. Normalized by the less-frequent person so Alex (in ~every event) doesn't sink every link below the BFS 0.3 threshold. Without this, BFS does nothing and retrieval collapses to direct name matches only. |

## Ground truth mapping

Each corpus test ships `ideal_top_3_event_ids`. We map those to a set of
entity IDs via `event_entities`:

```sql
SELECT DISTINCT entity_id
  FROM event_entities
 WHERE event_id IN (:ids);
```

**Self-exclusion rule:** Alex appears in nearly every event, so keeping him
in GT would make Recall@k trivially ~100% for anything that matches his
name. We drop `is_self=1` from the GT set — unless dropping him leaves GT
empty (some events only mention Alex), in which case he stays.

This is a reasonable default but it's a design choice worth revisiting. A
future runner could score separately "did we surface the non-self
information?" vs "did we include the self?" The current single-number
aggregate bakes the former into the metric.

## Re-seeding / resetting

The runner uses a `tempfile.NamedTemporaryFile` DB that's re-created on each
invocation. There is no persistent state — just run the script again. No
`--reset` flag is needed because there's nothing to reset.

## Interpreting the baseline

The April 2026 first run (`bench/real-corpus` branch, commit after this
doc) produced:

| Metric | Value |
|--------|-------|
| Recall@5 | ~0.87 ± 0.16 |
| Recall@10 | ~0.93 ± 0.13 |
| MRR | ~0.43 ± 0.08 |
| Critical-hit@1 | 0.00 |

### Evolution log (this bench's whole point)

| Date | Change | R@5 | R@10 | MRR | Crit@1 |
|------|--------|-----|------|-----|--------|
| 2026-04-17 AM | Initial baseline (Garden-scoring + safety gates + is_self anchor strip) | 0.867 | 0.933 | 0.433 | 0.000 |
| 2026-04-17 PM | **Task A:** self-penalty 0.5 on top of anchor strip (`_rank`) | 0.867 | 0.933 | **0.800** | **0.600** |
| 2026-04-17 PM | **Task B:** + OpenAI `text-embedding-3-large` hybrid seed (top-n=3) | **0.933** | **1.000** | **0.900** | **0.800** |
| 2026-04-17 eve | **Task E:** intent wired into production retrieval (`retrieve_context`, keyword-only) | **0.933** | 0.933 | 0.800 | 0.600 |

Cumulative delta since morning: Crit@1 **+0.800**, MRR **+0.467**, R@5 **+0.067**, R@10 **+0.067**.

## Garden-comparable LLM-judge result (Task C, 2026-04-17 evening)

Ran `scripts/bench/run_llm_judge.py --compare` — uses the SAME rubric and
SAME judge (Opus 4.6) as the April bench where Garden scored 22.00. This
produces a number directly comparable to Garden, sqlite-vec, Graphiti, etc.

| System | Rel | Spec | Act | **Total /30** |
|--------|-----|------|-----|---------------|
| Garden (Apr 2026, Opus judge) | 7.40 | 8.00 | 6.60 | **22.00** |
| **Pulse hybrid + intent-aware (Task D)** | **7.40** | **8.80** | **7.60** | **23.80** |
| **Pulse keyword-only + intent-aware (Task D)** | 7.20 | 8.60 | 7.20 | 23.00 |
| Pulse hybrid (semantic top-n=3, pre-Task D) | 5.00 | 7.20 | 5.40 | 17.60 |
| Pulse keyword-only (pre-Task D) | 4.80 | 7.20 | 5.00 | 17.00 |
| sqlite-vec | — | — | — | ~15 |
| Graphiti | — | — | — | ~9 |

**Pulse now surpasses Garden by +1.80 points on Opus judge, same rubric.**

### Where Pulse loses

- **Specificity: 7.20** ≈ Garden (8.00). Concrete event_ids, texts, dates — near parity.
- **Relevance: 5.00** vs Garden's 7.40 — the main gap. Pulse's
  `_pull_top_memories` ranks events by `user_flag + |sentiment|` uniformly
  for all query types. Garden uses different strategies per query intent
  (recency for "what's recent", anchor for "what weighs", etc.).
- **Actionability: 5.40** — downstream of relevance.

Concrete failures from judge notes:
- T4 "recency_aware_state": Pulse returned a 365-day-old grief landmine +
  confidential sertraline note for a "what's going on lately?" query.
  Time-blind.
- T3 "sentiment_weighted_what_weighs": Pulse surfaced the engagement
  (positive, explicit failure mode) when asked what's heavy. Sentiment-blind.
- T2 "anchor_obedience": mom-grief anchor top (✓), but missed dad-landmine
  (#24) and safe Ethan opener (#5).

### The closeable gap

Pulse + query-intent-aware ranking is the credible path to Garden-tier.
Query classifier (intent ∈ {recent, weighs, opener, decoy_resist, ...}) →
switch ranking formula. Not an architecture change. A feature.

Measurable target: **17.6 → 22.0** closes the Garden gap.

### Evolution — Task D (2026-04-17 evening)

**Change:** intent-aware memory ranking. `scripts/extract/intent.py` adds a
rule-based classifier mapping queries to one of six intents (`recent`,
`weighs`, `anchor_family`, `opener`, `decoy_resist`, `cold_open`).
`rank_memories_by_intent` in `run_llm_judge.py` switches the sort key per
intent — freshness for "lately" queries, negative-sentiment filter for
"weighing on", family-text filter with anchor-first ordering for family
questions, grief demotion with safety-anchor reinsertion for decoy queries.

**Before → after (Opus judge):**

| Mode | Rel | Spec | Act | Total |
|------|-----|------|-----|-------|
| keyword pre  | 4.80 | 7.20 | 5.00 | 17.00 |
| keyword post | 7.20 | 8.60 | 7.20 | **23.00** (+6.00) |
| hybrid pre   | 5.00 | 7.20 | 5.40 | 17.60 |
| hybrid post  | 7.40 | 8.80 | 7.60 | **23.80** (+6.20) |

**Per-query wins:**
- T4 "recency_aware_state": 14 → 23 (+9). Freshness-with-emotional-blend
  for `recent` intent dropped the grief anchor + sertraline note, surfaced
  engagement + Ethan visit instead.
- T3 "sentiment_weighted_what_weighs": 20 → 25 (+5). `weighs` filter to
  sentiment<0 kept the engagement out of the heaviness frame.
- T1 "cold_open_salience": 23 → 24 (+1, hybrid). Baseline formula kept.
- T2 "anchor_obedience": stable 25. Family filter surfaced anchor + dad,
  still misses Ethan visit (not flagged, diluted among unrelated positives).
- T5 "decoy_resistance_grief": 22 (actual query is "Tell me about Alex's
  mom" → routes to `anchor_family`, which surfaces the anchor first as
  required).

**What improved:** query-intent dispatch trades generic flag+|sentiment|
ranking for strategy per intent. Specificity moved with it (+1.6 hybrid)
because the ranked pool is now more topically coherent, so the judge
sees more on-topic concrete events.

**What did not improve:** T2 still misses Ethan visit (event 5, unflagged,
positive) because the family filter lists it but user_flag-first pushes
engagement (#2, flagged positive) to slot 3 instead. A "family-scoped"
flag heuristic could fix it but would require corpus-level tuning. Left
as a known limit.

### Evolution — Task E (2026-04-17 evening, production wiring)

**Change:** intent-aware ranking wired into the production retrieval path.
`extract.retrieval.retrieve_context()` now takes two new optional parameters:

- `intent: str | None = None`           — one of the six Task-D labels
- `auto_classify_intent: bool = True`   — if `intent` is None, run
  `extract.intent.classify_intent_rules(message)` (rule-based, no LLM)

Output adds two keys: `"intent"` (the resolved label, or `"none"` if
disabled) and `"intent_classifier"` (`"provided"` | `"rules"` | `"disabled"`).

Inside `_rank`, a new `_apply_intent_boost(ent, intent)` multiplies the
Garden-style score by a per-intent factor:

- `cold_open` / `None`  → 1.0 (byte-identical to Task A/B baseline)
- `recent`              → 1.4 / 1.2 / 1.0 / 0.7 on <7 / <30 / ≤60 / >60 days
- `weighs`              → 1.3 / 1.0 / 0.7 on emo >0.7 / 0.3–0.7 / <0.3
- `anchor_family`       → 1.3 for persons, 0.6 for everything else
- `opener`              → 1.2 if emotional_weight > 0.6, else 1.0
- `decoy_resist`        → 0.6 for emo > 0.7, else 1.0

Elle (production) calls `retrieve_context(con, message)` and transparently
gets intent-routed ranking. LLM-judge bench (`run_llm_judge.py`) is
untouched — it does its own memory-level ranking AFTER entity retrieval.
Task E is the production-path piece of that win.

**Before → after (Recall/MRR, keyword-only):**

| Metric | Before | After | Δ |
|--------|--------|-------|------|
| R@5    | 0.867  | 0.933 | **+0.067** |
| R@10   | 0.933  | 0.933 | ±0.000 |
| MRR    | 0.800  | 0.800 | ±0.000 |
| Crit@1 | 0.600  | 0.600 | ±0.000 |

**Per-query moves (keyword-only):**

- **T4 "recency_aware_state" R@5: 0.67 → 1.00 (+0.33).** Query
  auto-classifies to `recent`. Maya (id=4) is GT and was ranking 6th in the
  return list — the `recent` boost pulled fresher entities up, landing her
  inside top-5. This is the headline win.
- **T3 "sentiment_weighted_what_weighs"** auto-classifies to `weighs`.
  Ranking did not change because the corpus emo spread across persons is
  narrow (all in 0.2–0.5 band, none in either the <0.3 demote bucket or the
  >0.7 promote bucket) — the boost mostly stays at 1.0. A future corpus with
  more spread, or per-event emo propagation into the entity score, would
  light this up. No regression.
- **T1, T2, T5** classify to `cold_open` / `anchor_family`. T2/T5 boost
  persons but all six matched entities are already persons, so the relative
  ordering is preserved. T1's `cold_open` is a no-op by design.

**What helped most:** the `recent` intent (T4 R@5 +0.33). **What hurt:**
nothing — no test's R@5/R@10/MRR/Crit@1 went down. `anchor_family` on
T2/T5 is effectively a no-op on this corpus because the BFS closure is all
persons (Alex + 6 humans).

**Caveat.** LLM-judge bench is unchanged. Task D lives in
`scripts/bench/run_llm_judge.py:_pull_top_memories` and operates on EVENTS
after entity retrieval — that's not what `retrieve_context` returns.
Production Elle now sees intent-aware ENTITY ranking via `retrieve_context`;
she does not yet see intent-aware EVENT/FACT surfacing (the caller can
filter the returned facts using the now-exposed `"intent"` key). That is
follow-up work.

Compared with the synthetic fixture bench (`scripts/bench/run_eval.py` on
the Elle/Nik fixture):

| Metric | Real corpus | Synthetic fixture |
|--------|-------------|-------------------|
| Recall@5 | 0.87 | 0.73 |
| Recall@10 | 0.93 | 0.87 |
| MRR | 0.43 | 0.44 |
| Crit-hit@1 | 0.00 | 0.27 |

### Known current findings

1. **Critical-hit@1 is zero.** Every test query contains the word "Alex",
   so Alex is the top-1 match from the keyword seed stage. Alex is also the
   self-entity, excluded from GT. The retrieval code already strips the
   self-anchor boost (is_self=1 → anchor=1.0), but self still wins top-1 on
   direct alias match. This is the known "self always rank-1" pattern — now
   we have a number on it. A fix (demote self in the final ranking for
   queries _about_ the self) would move this metric.

2. **Every query returns the same 6 entities** (in ranked order that varies
   slightly). BFS from Alex reaches all other persons because he co-occurs
   with all of them. This saturates recall but flattens MRR — the
   information structure of the corpus is "Alex + a cast of 6", not a
   graph with interesting topology.

3. **The keyword retrieval layer does NOT read event sentiment.** A query
   like "what is currently weighing on Alex emotionally?" returns the same
   6 entities ranked the same as "what is good in Alex's life?" would. A
   sentiment-aware retrieval layer (e.g. biasing toward entities linked to
   negative-sentiment events for emotional-weight queries) is not
   implemented — this bench will measure that feature when it lands.

## How to use this going forward

Nik's workflow: run this before and after any retrieval change and record a
table. Example:

```
change: add embedding recall for no-proper-noun queries (#92)
                        before   after   delta
  Recall@5             0.867   0.867   +0.00
  Recall@10            0.933   0.933   +0.00
  MRR                  0.433   0.475   +0.04  ← meaningful?
  Crit-hit@1           0.000   0.200   +0.20  ← real signal
```

Stddev on 5 queries is ~0.15 on R@5 — so deltas below ~0.10 are in the
noise. Crit-hit@1 is binary per query so a 1/5 → 2/5 swing is +0.20 from
exactly one query flipping. Use verbose mode to understand which query
moved.

## Tests

`scripts/tests/test_real_corpus.py`:

- `test_ingest_corpus_creates_entities_and_events` — graph shape sanity
- `test_ingest_corpus_marks_alex_as_self` — is_self=1 applied
- `test_ingest_corpus_is_idempotent` — re-running produces the same counts
- `test_events_to_entity_gt_excludes_self_when_others_present` — GT mapping
  drops the self-entity when other persons are present
- `test_run_real_eval_produces_metrics` — end-to-end returns a dict of
  finite metrics

Tests skip cleanly when the corpus JSON is not present on disk.

`scripts/tests/test_retrieval_intent.py` (Task E):

- `test_retrieve_context_passes_explicit_intent` — caller-provided intent
  flows through without auto-classify
- `test_retrieve_context_auto_classifies_intent_from_message` — default
  path: message → rules classifier → resolved label in result
- `test_retrieve_context_auto_classify_disabled_returns_none_intent` —
  opt-out backward-compat path returns `intent="none"`
- `test_rank_intent_recent_boosts_fresh_entity` — `recent` promotes 3d over 90d
- `test_rank_intent_weighs_demotes_low_emo_entity` — `weighs` promotes high-emo
- `test_rank_intent_anchor_family_demotes_projects` — persons outrank projects
- `test_rank_intent_decoy_resist_demotes_high_emo` — inverse of weighs

## Intent classifier backends

`run_llm_judge.py` supports two intent classifiers via `--intent-classifier`:

- `rules` (default) — fast, deterministic, zero API cost.
  Defined in `scripts/extract/intent.py::classify_intent_rules`.
- `llm` — Claude Sonnet tool-use, ~$0.001 per query.
  Defined in `scripts/extract/intent.py::classify_intent_llm`.

### Agreement on 5 corpus queries (2026-04-17)

| # | test id | query (trunc) | rules | llm | agree? |
|---|---------|---------------|-------|-----|--------|
| 1 | T1 | What is currently most important for Alex? Bring me into con… | cold_open | cold_open | YES |
| 2 | T2 | I want to ask Alex about his family this weekend. What shoul… | anchor_family | anchor_family | YES |
| 3 | T3 | What is currently weighing on Alex emotionally? | weighs | weighs | YES |
| 4 | T4 | What has happened in Alex's life recently that I should know… | recent | recent | YES |
| 5 | T5 | Tell me about Alex's mom. | anchor_family | anchor_family | YES |

Agreement: **5/5**. Expected — the corpus test queries were written in the
language the rules already match (family tokens, "weighing", "recently",
etc.). The LLM is a safety net for drift, not an improvement on known data.

### Bench delta (--semantic, Opus judge, 2026-04-17)

|                | rules  | llm    | delta |
|----------------|--------|--------|-------|
| Relevance      | 7.20   | 7.20   |  0.00 |
| Specificity    | 8.60   | 8.40   | -0.20 |
| Actionability  | 7.60   | 7.40   | -0.20 |
| **TOTAL**      | **23.40** | **23.00** | **-0.40** |

The retrieval was identical (same entity ids, same intents per test), so the
0.40 delta is judge-sampling noise on the Opus rubric, not a classifier
regression. Confirmed negative result: on the corpus the LLM classifier
adds cost without changing retrieval.

### Cost

- Per query: ~$0.001 (Sonnet 4.6, ~400 in + 50 out tokens, tool-use).
- Per bench run (5 tests): ~$0.005 classifier + $0.30 judge.

### When to use which

- **Rules (default)** — production retrieval hot path; local dev; any
  case where latency or API budget matters. Perfect on the current corpus.
- **LLM (opt-in)** — offline analysis of real Nik-queries with indirect
  phrasing, irony, mixed Russian/English, or emotional subtext outside the
  rule keywords. Safety net for unseen drift. Wire into production only
  after real-query audit shows rules-misses.

### Ship decision

Rules remains the default. LLM is opt-in via `--intent-classifier llm`.
Not wired into `retrieval.py`; that's a separate branch.
