# Pulse retrieval bench

A reusable, fixture-driven harness that scores `extract.retrieval.retrieve_context()`
against a held-out query set. Quantitative only — no LLM-as-judge, no external
bench integration. The goal is a cheap, deterministic number we can move when
we touch retrieval code.

## Why this exists

In April 2026 a 9-way empathic memory bench ran externally. **Garden** (custom
salience-based retrieval, no graph) won with 26.71. **Graphiti** (graph-heavy,
no salience) came last with 6.77. Pulse did not participate.

Then Phase 2e shipped (BFS, salience decay, co-occurrence, gaps) and the
Garden-style `_rank` rewrite (emotional_weight + anchor + kind-aware exp decay).
106 unit tests verify properties — hop penalty, tokenization — but none measure
**retrieval quality**: does Pulse surface the memory Elle actually needs?

This harness is the smallest possible answer to that question.

## Run

```bash
python scripts/bench/run_eval.py            # summary + by-category table
python scripts/bench/run_eval.py --verbose  # + per-query breakdown
```

Exits 0 on success. Uses only stdlib — no extra deps.

## Metrics

| metric | definition |
|--------|------------|
| **Recall@k** | `|top-k ∩ ground_truth| / |ground_truth|`, averaged over queries |
| **MRR** | mean of `1 / rank_of_first_correct`; 0 if none in top-10 |
| **Critical-hit** | fraction of queries where `top-1 ∈ ground_truth` |

Reported as `mean ± stddev` (population stddev of per-query values — the
fixture is small, bootstrap was overkill).

## Fixture

- **`fixtures/empathic_corpus.py`** — 18 entities across `person / project /
  place / concept / org / thing`, with realistic emotional_weight (0.05 → 0.92)
  and `last_seen` spanning fresh (0d) to stale (110d). Includes 3 close
  relationships (Anna/Sonya/Nik), 2 trauma-adjacent entities (Kristina,
  Anna-receiver-wound), and 18 hand-authored relations + 11 facts.
- **`fixtures/queries.py`** — 15 queries tagged with categories: `direct-name`,
  `alias`, `alias-trauma`, `multi-direct`, `2-hop-relative`, `2-hop-via-place`,
  `2-hop-project`, `emotional-alias`, `emotional-gap`, `freshness-fresh`,
  `freshness-stale`, `place`, `thing`, `direct-alias-self`.

Seeding bypasses `pulse_extract.py` (LLM calls, too slow/expensive for repeated
runs) and writes directly into the entities/relations/facts tables after
applying every migration in `internal/store/migrations/`.

## 2026-04-16 baseline

Run against commit-at-worktree-branch `bench-harness-a75b1111` (off `main @
39e6c84`). Retrieval called with `top_k=10, depth=2`.

```
SUMMARY  (n=15 queries)
  Recall@5       0.733 ± 0.442
  Recall@10      0.867 ± 0.340
  MRR            0.439 ± 0.363
  Critical-hit   0.267 ± 0.442
```

Aggregate numbers lie. The per-category breakdown tells the story:

```
BY CATEGORY
  category                  n     R@5    R@10     MRR    crit
  2-hop-project             1   1.000   1.000   0.250   0.000
  2-hop-relative            1   0.000   0.000   0.000   0.000   <- Ani→Fedya fails
  2-hop-via-place           1   1.000   1.000   0.500   0.000
  alias                     1   1.000   1.000   1.000   1.000
  alias-trauma              1   1.000   1.000   0.500   0.000
  direct-alias-self         1   1.000   1.000   1.000   1.000
  direct-name               1   1.000   1.000   1.000   1.000
  emotional-alias           2   1.000   1.000   0.250   0.000
  emotional-gap             1   0.000   0.000   0.000   0.000   <- "пусто сегодня"
  freshness-fresh           1   1.000   1.000   0.250   0.000
  freshness-stale           1   1.000   1.000   1.000   1.000
  multi-direct              1   1.000   1.000   0.250   0.000
  place                     1   0.000   1.000   0.167   0.000
  thing                     1   0.000   1.000   0.167   0.000
```

### Findings this run surfaced

1. **Nik (self-entity) dominates top-1.** Any query that expands through BFS
   hits Nik (salience=1.0, emo=0.9, anchor=1.5) and he outranks the actually
   named entity. Recall stays high but `Critical-hit = 0.267` — Elle gets the
   right context in top-10 but the top-1 slot is almost always "Nik" even when
   the user said "Кристина" or "Garden". **Likely fix**: deprioritize the
   self-entity, or drop it from BFS expansion, or downweight it post-rank.

2. **Russian declension breaks 2-hop anchors.** Query `"Как там сын Ани?"`
   tokenizes `Ани` (genitive of Аня). The alias list `["Аня", "Анна", "жена"]`
   does not include `Ани` → no anchor match → BFS cannot fire → Fedya is
   unreachable. This is the single biggest recall gap: the entity IS in the
   graph, 1 hop from a known alias, and we still miss it. **Phase 3 target**:
   either richer alias generation (morphological forms) or embeddings.

3. **`emotional-gap` confirmed as expected.** Query `"пусто сегодня, ничего не
   хочется"` has no token in any alias list → empty result. This is the known
   keyword-retrieval weakness; documented here so Phase 3 (embeddings) has a
   concrete benchmark to beat: drive this category from 0 → non-zero.

4. **`emotional-alias` works but ranks badly.** `"мне плохо"` (literally in the
   `loneliness` alias list) is found, but ranked 4th behind Nik/Anna/Sonya
   because the anchor boost + self-entity dominance wins. MRR=0.25. Concepts
   in this fixture have `kind="concept"` with `λ=0.01` → fast decay, no anchor
   boost. Trade-off of the current ranker.

5. **Freshness ordering works as designed.** The `freshness-stale` query
   (Krisp, 110d old) is recalled at rank 1 because no other entity token-matches
   `"Krisp"`. The `freshness-fresh` query pulls in Pulse fresh and Krisp stale
   simultaneously — Pulse does outrank Krisp (as desired) once both are in the
   result set.

## Extending with a real corpus

The harness is deliberately fixture-agnostic:

- Swap `fixtures/empathic_corpus.py::ENTITIES/RELATIONS/FACTS` for data dumped
  from a real Pulse DB (or a script that reads one). The `seed()` signature
  (takes a `sqlite3.Connection`) does not change.
- Add queries to `fixtures/queries.py` — each needs only `id`, `message`,
  `ground_truth: list[int]`, `category: str`.
- Nothing in `run_eval.py` is corpus-specific.

A reasonable Phase 3 plan: dump 200 real observations, label 30-50 held-out
queries from chat-exports, re-run. Compare `Critical-hit` and
`emotional-gap`-category recall before/after embeddings.

## What this harness is NOT

- Not an LLM-as-judge bench. No quality scoring of Elle's response — only
  retrieval hits.
- Not integrated with `~/dev/ai/bench/`. Intentionally local and cheap.
- Not a regression gate for CI (yet). If we want that, pick a threshold on
  `Critical-hit` and fail the run below it.
