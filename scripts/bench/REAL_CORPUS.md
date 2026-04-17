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

Cumulative delta since morning: Crit@1 **+0.800**, MRR **+0.467**, R@5 **+0.067**, R@10 **+0.067**.

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
