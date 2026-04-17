"""Pulse retrieval benchmark harness.

Usage:
    python scripts/bench/run_eval.py           # summary table only
    python scripts/bench/run_eval.py --verbose # + per-query breakdown

Loads the fixture corpus (scripts/bench/fixtures/empathic_corpus.py), seeds
it into an in-memory SQLite graph via raw inserts (NOT the LLM extract
pipeline — that is too slow/expensive for repeated runs), and evaluates
retrieve_context() against the held-out queries in
scripts/bench/fixtures/queries.py.

Metrics:
    Recall@5       = |top5 ∩ gt| / |gt|, averaged over queries
    Recall@10      = |top10 ∩ gt| / |gt|, averaged over queries
    MRR            = mean of 1/rank_of_first_correct (0 if none in top-10)
    Critical-hit   = fraction of queries where top-1 ∈ gt

Each metric is reported as mean ± stddev across queries (stddev of per-query
values — this is a small fixture, bootstrap was overkill).

Exits 0 on success. Non-zero exit only on unexpected errors (empty result
sets are a finding, not a failure).
"""

import argparse
import math
import sqlite3
import statistics
import sys
from pathlib import Path

# scripts/ layout: make 'extract' and 'bench' importable when run as a script
_HERE = Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from bench.fixtures.empathic_corpus import seed as seed_corpus  # noqa: E402
from bench.fixtures.queries import QUERIES  # noqa: E402
from extract.retrieval import retrieve_context  # noqa: E402
from pulse_consolidate import embed_entities  # noqa: E402


def _fresh_db() -> sqlite3.Connection:
    """In-memory SQLite, all Pulse migrations applied in order."""
    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA foreign_keys = ON")
    migrations = _SCRIPTS.parent / "internal" / "store" / "migrations"
    for sql in sorted(migrations.glob("*.sql")):
        con.executescript(sql.read_text())
    return con


def _rank_of_first_correct(returned_ids: list[int], gt: set[int]) -> int | None:
    for rank, eid in enumerate(returned_ids, start=1):
        if eid in gt:
            return rank
    return None


def _run_query(
    con, q: dict, top_k: int = 10, depth: int = 2,
    semantic: bool = False, embedder_model: str = "fake-local",
) -> dict:
    result = retrieve_context(
        con, q["message"], top_k=top_k, depth=depth,
        semantic=semantic, embedder_model=embedder_model,
    )
    returned = [e["id"] for e in result["matched_entities"]]
    gt = set(q["ground_truth"])

    top5 = set(returned[:5])
    top10 = set(returned[:10])

    recall_5 = len(top5 & gt) / len(gt) if gt else 0.0
    recall_10 = len(top10 & gt) / len(gt) if gt else 0.0
    first_rank = _rank_of_first_correct(returned, gt)
    mrr = (1.0 / first_rank) if first_rank is not None else 0.0
    crit_hit = 1.0 if returned and returned[0] in gt else 0.0

    return {
        "id": q["id"],
        "message": q["message"],
        "category": q["category"],
        "ground_truth": sorted(gt),
        "returned": returned,
        "top1_name": result["matched_entities"][0]["canonical_name"] if returned else None,
        "total_matched": result["total_matched"],
        "recall_5": recall_5,
        "recall_10": recall_10,
        "mrr": mrr,
        "crit_hit": crit_hit,
        "retrieval_method": result.get("retrieval_method", "keyword"),
        "semantic_seeds": result.get("semantic_seeds", []),
    }


def _summarize(per_query: list[dict]) -> dict:
    def _ms(key: str) -> tuple[float, float]:
        vals = [q[key] for q in per_query]
        mean = statistics.fmean(vals)
        stdev = statistics.pstdev(vals) if len(vals) > 1 else 0.0
        return mean, stdev

    r5_m, r5_s = _ms("recall_5")
    r10_m, r10_s = _ms("recall_10")
    mrr_m, mrr_s = _ms("mrr")
    ch_m, ch_s = _ms("crit_hit")

    return {
        "n": len(per_query),
        "recall_5": (r5_m, r5_s),
        "recall_10": (r10_m, r10_s),
        "mrr": (mrr_m, mrr_s),
        "crit_hit": (ch_m, ch_s),
    }


def _print_verbose(per_query: list[dict]) -> None:
    print()
    print("=" * 78)
    print("PER-QUERY BREAKDOWN")
    print("=" * 78)
    for q in per_query:
        print(f"\n[{q['id']}]  category={q['category']}")
        print(f"  msg: {q['message']!r}")
        print(f"  ground_truth: {q['ground_truth']}")
        print(f"  returned (top-10): {q['returned']}")
        print(f"  top1: {q['top1_name']!r}  total_matched={q['total_matched']}")
        print(
            f"  R@5={q['recall_5']:.2f}  R@10={q['recall_10']:.2f}  "
            f"MRR={q['mrr']:.2f}  crit={int(q['crit_hit'])}"
        )


def _print_summary(summary: dict) -> None:
    r5_m, r5_s = summary["recall_5"]
    r10_m, r10_s = summary["recall_10"]
    mrr_m, mrr_s = summary["mrr"]
    ch_m, ch_s = summary["crit_hit"]
    n = summary["n"]

    print()
    print("=" * 78)
    print(f"SUMMARY  (n={n} queries)")
    print("=" * 78)
    print(f"  Recall@5       {r5_m:.3f} ± {r5_s:.3f}")
    print(f"  Recall@10      {r10_m:.3f} ± {r10_s:.3f}")
    print(f"  MRR            {mrr_m:.3f} ± {mrr_s:.3f}")
    print(f"  Critical-hit   {ch_m:.3f} ± {ch_s:.3f}")
    print()


def _print_category_breakdown(per_query: list[dict]) -> None:
    """Small extra table: metrics grouped by category. Useful for spotting the
    "emotional-gap" category collapsing even when aggregates look OK."""
    by_cat: dict[str, list[dict]] = {}
    for q in per_query:
        by_cat.setdefault(q["category"], []).append(q)

    print("BY CATEGORY")
    print("-" * 78)
    print(f"  {'category':<22}  {'n':>3}  {'R@5':>6}  {'R@10':>6}  {'MRR':>6}  {'crit':>6}")
    for cat, rows in sorted(by_cat.items()):
        n = len(rows)
        r5 = statistics.fmean(r["recall_5"] for r in rows)
        r10 = statistics.fmean(r["recall_10"] for r in rows)
        mrr = statistics.fmean(r["mrr"] for r in rows)
        ch = statistics.fmean(r["crit_hit"] for r in rows)
        print(f"  {cat:<22}  {n:>3}  {r5:>6.3f}  {r10:>6.3f}  {mrr:>6.3f}  {ch:>6.3f}")
    print()


def _delta(a: float, b: float) -> str:
    """Format a signed delta for the comparison table."""
    d = b - a
    sign = "+" if d >= 0 else ""
    return f"{sign}{d:.3f}"


def _print_hybrid_comparison(
    baseline: list[dict], hybrid: list[dict]
) -> None:
    """Side-by-side keyword-only vs hybrid (keyword+semantic) comparison."""
    bs = _summarize(baseline)
    hs = _summarize(hybrid)
    print("=" * 78)
    print(f"HYBRID COMPARISON  (n={bs['n']} queries)")
    print("=" * 78)
    print(f"  {'metric':<14} {'keyword':>10}  {'hybrid':>10}  {'delta':>10}")
    for key, label in [
        ("recall_5", "Recall@5"),
        ("recall_10", "Recall@10"),
        ("mrr", "MRR"),
        ("crit_hit", "Critical-hit"),
    ]:
        b_m = bs[key][0]
        h_m = hs[key][0]
        print(f"  {label:<14} {b_m:>10.3f}  {h_m:>10.3f}  {_delta(b_m, h_m):>10}")
    print()

    # Specifically call out the two known-failure queries. These are the
    # queries Judge 4 (rival engineer) and Judge 1 (A/B designer) flagged
    # as the retrieval weakness keyword can't close.
    focus_ids = {"q05_2hop_relative", "q08_emo_no_token_weakness"}
    baseline_by_id = {q["id"]: q for q in baseline}
    hybrid_by_id = {q["id"]: q for q in hybrid}
    print("KNOWN-FAILURE QUERIES  (keyword → hybrid)")
    print("-" * 78)
    for qid in sorted(focus_ids):
        b = baseline_by_id.get(qid)
        h = hybrid_by_id.get(qid)
        if not b or not h:
            continue
        print(f"\n[{qid}]  msg={b['message']!r}")
        print(f"  ground_truth : {b['ground_truth']}")
        print(f"  keyword  top-10 ids: {b['returned']}  (R@10={b['recall_10']:.2f})")
        print(f"  hybrid   top-10 ids: {h['returned']}  (R@10={h['recall_10']:.2f})")
        print(f"  hybrid   semantic_seeds: {h['semantic_seeds']}")
    print()
    # Plumbing vs semantics disclaimer — fake-local is hash-based so any
    # movement on q05/q08 is coincidence, not actual semantic recall.
    print(
        "NOTE: `fake-local` embeddings are deterministic hashes, NOT semantic. "
        "This run validates the plumbing (embed → store → cosine → seed "
        "union → BFS → rank) end-to-end. Real semantic gains require "
        "switching `--embedder-model` to `openai-text-embedding-3-large` "
        "(or a local model) once Nik green-lights the API spend."
    )
    print()


def main() -> int:
    p = argparse.ArgumentParser(description="Pulse retrieval quality bench")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="print per-query breakdown")
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--depth", type=int, default=2,
                   help="BFS depth for retrieval (2 is default so 2-hop queries can succeed)")
    p.add_argument(
        "--semantic", action="store_true",
        help="Run the hybrid (keyword+semantic) pipeline AND compare to "
             "keyword-only baseline. Embeds seeded entities with --embedder-model "
             "(default 'fake-local', no API calls).",
    )
    p.add_argument(
        "--embedder-model", default="fake-local",
        help="Embedding backend for --semantic. Options: "
             "'fake-local' (deterministic hash, no API, DEFAULT), "
             "'openai-text-embedding-3-large' (requires OPENAI_API_KEY).",
    )
    args = p.parse_args()

    con = _fresh_db()
    seed_corpus(con)

    baseline = [_run_query(con, q, top_k=args.top_k, depth=args.depth) for q in QUERIES]

    if args.verbose:
        _print_verbose(baseline)

    print()
    _print_category_breakdown(baseline)

    summary = _summarize(baseline)
    _print_summary(summary)

    if args.semantic:
        # Populate entity_embeddings BEFORE running the hybrid pass.
        # Default only_missing=True — first run embeds all 18 fixture entities.
        n_embedded = embed_entities(con, embedder_model=args.embedder_model)
        print(f"[semantic] embedded {n_embedded} entities with {args.embedder_model!r}")
        print()

        hybrid = [
            _run_query(
                con, q, top_k=args.top_k, depth=args.depth,
                semantic=True, embedder_model=args.embedder_model,
            )
            for q in QUERIES
        ]

        if args.verbose:
            _print_verbose(hybrid)

        _print_category_breakdown(hybrid)
        _print_hybrid_comparison(baseline, hybrid)

    # Sanity: every query produced a dict with numeric metrics — non-empty output
    assert all(math.isfinite(q["recall_5"]) for q in baseline)
    return 0


if __name__ == "__main__":
    sys.exit(main())
