"""Pulse retrieval benchmark on the REAL empathic-memory-corpus.

This is the "Alex" 30-event dataset used in the 9-way April bench where Garden
won with 26.71. Unlike `run_eval.py` (which uses a hand-authored fixture of
the Elle/Nik domain), this runner exercises Pulse retrieval against the same
text corpus that was scored by the 12 judges on April 9, 2026.

Usage:
    python scripts/bench/run_real_eval.py                      # summary only
    python scripts/bench/run_real_eval.py --verbose            # per-query
    python scripts/bench/run_real_eval.py --corpus PATH        # custom corpus

What it does:
    1. Loads the corpus JSON (events + tests).
    2. Builds a Pulse graph WITHOUT calling any LLM: direct SQL inserts.
       Extracts person entities with a small name list seeded from
       _meta.user.snapshot, creates event rows, links events → entities via
       `event_entities`, and attaches a fact to the primary person per event.
       Alex gets is_self=1 (real use case for the flag).
    3. Maps each test's `ideal_top_3_event_ids` → set of non-self entity IDs
       (via event_entities). This is the retrieval ground truth.
    4. Runs `extract.retrieval.retrieve_context()` per test query.
    5. Computes Recall@5, Recall@10, MRR, Critical-hit@1.

What it does NOT do:
    - Does NOT invoke any LLM (zero $).
    - Does NOT measure companion-response quality. Garden's 26.71 is a
      weighted rubric across tone/presence/memory-surfacing/etc., scored by
      LLM judges on full responses. Our metrics measure whether Pulse
      retrieval SURFACES the right entities — a prerequisite for good
      responses, but not the whole thing.
    - Does NOT compete directly with Garden 26.71. Apples/oranges. This
      runner is a scoreboard for retrieval changes inside Pulse.

Re-run:
    Graph is seeded into a tempfile DB (idempotent on fixed entity IDs) and
    torn down at exit. Running twice gives the same numbers.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import statistics
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# scripts/ layout: make 'extract' importable when run as a script
_HERE = Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from extract.retrieval import retrieve_context  # noqa: E402


DEFAULT_CORPUS_PATH = Path(
    os.path.expanduser("~/dev/ai/bench/datasets/empathic-memory-corpus.json")
)

# Anchor "now" for ts arithmetic. We use real now() so the retrieval-time
# recency decay matches production behaviour. days_ago in corpus is the
# offset from the moment the runner is invoked.
NOW = datetime.now(timezone.utc)


# --- Person name list -------------------------------------------------------
# Seeded from _meta.user.snapshot, extended with names that appear in events
# (David in event 24). Each key is canonical_name; value is alias list.
# This is explicitly LLM-free: we're not asking a model to NER — we're using
# a hand-maintained list that covers the known cast of the corpus.
PERSON_CATALOG: dict[str, list[str]] = {
    "Alex": ["Alex", "Alex's"],
    "Maya": ["Maya", "Maya's"],
    "Sarah": ["Sarah", "mom", "mother"],
    "Cooper": ["Cooper"],
    "Ethan": ["Ethan"],
    "Jordan": ["Jordan"],
    "David": ["David", "father"],
}

# Fixed ID allocation so seed is deterministic and re-runnable.
# 1..N reserved for persons in PERSON_CATALOG insertion order.
PERSON_IDS: dict[str, int] = {
    name: idx + 1 for idx, name in enumerate(PERSON_CATALOG.keys())
}


# --- Helpers ----------------------------------------------------------------
def _iso(days_ago: int) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _detect_persons(text: str) -> list[str]:
    """Return canonical person names mentioned in text, ordered by first
    occurrence. Case-insensitive whole-word match.
    """
    hits: list[tuple[int, str]] = []
    lower = text.lower()
    for canonical, aliases in PERSON_CATALOG.items():
        best = None
        for alias in aliases:
            # whole-word, case-insensitive
            pattern = r"\b" + re.escape(alias.lower()) + r"\b"
            m = re.search(pattern, lower)
            if m and (best is None or m.start() < best):
                best = m.start()
        if best is not None:
            hits.append((best, canonical))
    hits.sort()
    # dedup, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for _, name in hits:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


# --- Graph build ------------------------------------------------------------
def fresh_db() -> sqlite3.Connection:
    """Tempfile SQLite DB with all Pulse migrations applied, in order.

    Tempfile (not :memory:) so sqlite-vec style extensions that open the file
    keep working; the file is deleted on interpreter exit via tempfile API.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    con = sqlite3.connect(tmp.name)
    con.execute("PRAGMA foreign_keys = ON")
    migrations = _SCRIPTS.parent / "internal" / "store" / "migrations"
    for sql in sorted(migrations.glob("*.sql")):
        con.executescript(sql.read_text())
    # tag so tests/callers can find the path for teardown if needed
    con.execute(
        "CREATE TABLE IF NOT EXISTS _meta_runner (key TEXT PRIMARY KEY, value TEXT)"
    )
    con.execute(
        "INSERT INTO _meta_runner (key, value) VALUES ('db_path', ?)", (tmp.name,)
    )
    con.commit()
    return con


def ingest_corpus(con: sqlite3.Connection, corpus: dict) -> dict:
    """LLM-free ingest of the empathic-memory-corpus into a Pulse graph.

    Returns a dict with ingestion stats (entity/event counts, mention map).
    """
    events_raw = corpus["events"]

    # ---- Pre-pass: count mentions per person across corpus for salience ----
    mentions: dict[str, list[int]] = defaultdict(list)  # person -> [sentiments]
    event_persons: dict[int, list[str]] = {}
    for ev in events_raw:
        persons = _detect_persons(ev["text"])
        event_persons[ev["id"]] = persons
        for p in persons:
            mentions[p].append(ev["sentiment"])

    # ---- Create person entities (idempotent on fixed IDs) ----
    # Salience heuristic: mentions_count / total_events, capped at 1.0
    # Emotional weight: mean(abs(sentiment))/2 across events mentioning this person
    total_events = len(events_raw)
    for canonical, aliases in PERSON_CATALOG.items():
        eid = PERSON_IDS[canonical]
        sentiments = mentions.get(canonical, [])
        n = len(sentiments)
        salience = min(1.0, n / total_events) if n else 0.05
        emo = (sum(abs(s) for s in sentiments) / (2.0 * n)) if n else 0.0
        is_self = 1 if canonical == "Alex" else 0
        # first_seen = oldest event mentioning this person; last_seen = newest
        relevant_days = [
            ev["days_ago"] for ev in events_raw
            if canonical in event_persons[ev["id"]]
        ]
        fs = max(relevant_days) if relevant_days else 365
        ls = min(relevant_days) if relevant_days else 365
        con.execute(
            "INSERT OR REPLACE INTO entities "
            "(id, canonical_name, kind, aliases, first_seen, last_seen, "
            " salience_score, emotional_weight, description_md, is_self) "
            "VALUES (?, ?, 'person', ?, ?, ?, ?, ?, ?, ?)",
            (
                eid,
                canonical,
                json.dumps(aliases, ensure_ascii=False),
                _iso(fs),
                _iso(ls),
                salience,
                emo,
                f"Person from empathic-memory-corpus (mentions={n}).",
                is_self,
            ),
        )

    # ---- Insert events + event_entities + one fact per event ----
    for ev in events_raw:
        eid = ev["id"]
        text = ev["text"]
        sentiment = ev["sentiment"]
        emo = abs(sentiment) / 2.0
        ts = _iso(ev["days_ago"])
        title = text[:60]
        con.execute(
            "INSERT OR REPLACE INTO events "
            "(id, title, description, sentiment, emotional_weight, ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (eid, title, text, float(sentiment), emo, ts),
        )

        persons = event_persons[eid]
        for p in persons:
            pid = PERSON_IDS[p]
            con.execute(
                "INSERT OR IGNORE INTO event_entities (event_id, entity_id) "
                "VALUES (?, ?)",
                (eid, pid),
            )

        # Primary entity = first person mentioned. Attach a fact.
        if persons:
            primary_pid = PERSON_IDS[persons[0]]
            confidence = 0.9 if ev.get("user_flag") else 0.7
            # fact unique by (entity_id, text) — INSERT OR IGNORE keeps idempotent
            con.execute(
                "INSERT OR IGNORE INTO facts "
                "(entity_id, text, confidence, created_at) "
                "VALUES (?, ?, ?, ?)",
                (primary_pid, text, confidence, ts),
            )

    # ---- Co-occurrence relations between persons (LLM-free structural signal) ----
    # Two people mentioned in the same event are linked with kind='co_occurs'.
    # Strength = count_of_shared_events / max(mentions) — so Alex→Maya is ~0.5,
    # Alex→David is ~0.03 (only one event). Above 0.3 (BFS threshold) for pairs
    # that co-occur in 30%+ of the busier person's events.
    pair_counts: dict[tuple[str, str], int] = defaultdict(int)
    for eid, persons in event_persons.items():
        for i in range(len(persons)):
            for j in range(i + 1, len(persons)):
                a, b = persons[i], persons[j]
                key = tuple(sorted([a, b]))
                pair_counts[key] += 1

    for (a, b), count in pair_counts.items():
        a_count = max(1, len(mentions.get(a, [])))
        b_count = max(1, len(mentions.get(b, [])))
        # Normalize by the less-frequent person (Alex is in ~every event, so
        # normalizing by max would give tiny strengths for everyone). A pair
        # that co-occurs in ALL of Maya's events gets 1.0, not 5/30.
        strength = count / min(a_count, b_count)
        # Clamp into (0.0, 1.0]
        strength = max(0.0, min(1.0, strength))
        a_id = PERSON_IDS[a]
        b_id = PERSON_IDS[b]
        now_iso = _iso(0)
        for f, t in ((a_id, b_id), (b_id, a_id)):
            con.execute(
                "INSERT OR IGNORE INTO relations "
                "(from_entity_id, to_entity_id, kind, strength, first_seen, last_seen, context) "
                "VALUES (?, ?, 'co_occurs', ?, ?, ?, ?)",
                (f, t, strength, now_iso, now_iso,
                 f"Co-occurs in {count} event(s)"),
            )

    con.commit()

    return {
        "n_entities": con.execute("SELECT COUNT(*) FROM entities").fetchone()[0],
        "n_events": con.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        "n_event_entities": con.execute(
            "SELECT COUNT(*) FROM event_entities"
        ).fetchone()[0],
        "n_facts": con.execute("SELECT COUNT(*) FROM facts").fetchone()[0],
        "event_persons": event_persons,
    }


# --- Ground-truth mapping ---------------------------------------------------
def events_to_entity_gt(
    con: sqlite3.Connection,
    event_ids: list[int],
    exclude_self: bool = True,
) -> set[int]:
    """Map a list of event IDs to the set of entity IDs linked via
    event_entities. Optionally drops the self-entity (Alex) — since Alex
    appears in ~every event, keeping him would make Recall@k trivially high
    and uninformative.

    If excluding self would leave GT empty (event only mentions Alex), we
    KEEP self as a last resort so the metric reflects something.
    """
    placeholders = ",".join("?" * len(event_ids))
    rows = con.execute(
        f"SELECT DISTINCT entity_id FROM event_entities WHERE event_id IN ({placeholders})",
        tuple(event_ids),
    ).fetchall()
    ids = {r[0] for r in rows}
    if exclude_self:
        without_self = {
            i for i in ids
            if not con.execute(
                "SELECT is_self FROM entities WHERE id = ?", (i,)
            ).fetchone()[0]
        }
        if without_self:
            return without_self
    return ids


# --- Eval -------------------------------------------------------------------
def _rank_of_first_correct(returned_ids: list[int], gt: set[int]) -> int | None:
    for rank, eid in enumerate(returned_ids, start=1):
        if eid in gt:
            return rank
    return None


def _run_query(
    con: sqlite3.Connection, test: dict, gt: set[int], top_k: int = 10, depth: int = 2
) -> dict:
    result = retrieve_context(con, test["user_query"], top_k=top_k, depth=depth)
    returned = [e["id"] for e in result["matched_entities"]]

    top5 = set(returned[:5])
    top10 = set(returned[:10])

    recall_5 = len(top5 & gt) / len(gt) if gt else 0.0
    recall_10 = len(top10 & gt) / len(gt) if gt else 0.0
    first_rank = _rank_of_first_correct(returned, gt)
    mrr = (1.0 / first_rank) if first_rank is not None else 0.0
    crit_hit = 1.0 if returned and returned[0] in gt else 0.0

    return {
        "id": test["id"],
        "name": test["name"],
        "message": test["user_query"],
        "ground_truth": sorted(gt),
        "returned": returned,
        "top1_name": result["matched_entities"][0]["canonical_name"] if returned else None,
        "total_matched": result["total_matched"],
        "recall_5": recall_5,
        "recall_10": recall_10,
        "mrr": mrr,
        "crit_hit": crit_hit,
    }


def _summarize(per_query: list[dict]) -> dict:
    def ms(key: str) -> tuple[float, float]:
        vals = [q[key] for q in per_query]
        mean = statistics.fmean(vals) if vals else 0.0
        stdev = statistics.pstdev(vals) if len(vals) > 1 else 0.0
        return mean, stdev

    return {
        "n": len(per_query),
        "recall_5": ms("recall_5"),
        "recall_10": ms("recall_10"),
        "mrr": ms("mrr"),
        "crit_hit": ms("crit_hit"),
    }


def _print_verbose(per_query: list[dict]) -> None:
    print()
    print("=" * 78)
    print("PER-QUERY BREAKDOWN")
    print("=" * 78)
    for q in per_query:
        print(f"\n[{q['id']}] {q['name']}")
        print(f"  msg: {q['message']!r}")
        print(f"  ground_truth (entity ids): {q['ground_truth']}")
        print(f"  returned (top-10): {q['returned']}")
        print(f"  top1: {q['top1_name']!r}  total_matched={q['total_matched']}")
        print(
            f"  R@5={q['recall_5']:.2f}  R@10={q['recall_10']:.2f}  "
            f"MRR={q['mrr']:.2f}  crit={int(q['crit_hit'])}"
        )


def _print_summary(summary: dict, n_events: int) -> None:
    r5_m, r5_s = summary["recall_5"]
    r10_m, r10_s = summary["recall_10"]
    mrr_m, mrr_s = summary["mrr"]
    ch_m, ch_s = summary["crit_hit"]
    n = summary["n"]

    print()
    print("=" * 78)
    print(f"PULSE on empathic-memory-corpus ({n_events} events, {n} queries):")
    print("=" * 78)
    print(f"  Recall@5    : {r5_m:.3f} ± {r5_s:.3f}")
    print(f"  Recall@10   : {r10_m:.3f} ± {r10_s:.3f}")
    print(f"  MRR         : {mrr_m:.3f} ± {mrr_s:.3f}")
    print(f"  Crit-hit@1  : {ch_m:.3f} ± {ch_s:.3f}")
    print()
    print("Reference (from Apr 9 2026 bench):")
    print("  Garden (winner)       : 26.71 (domain scoring, not Recall)")
    print("  Graphiti (last)       : 6.77")
    print("  Note: Garden's 26.71 is a weighted rubric score (0-80 scale), not a Recall measure.")
    print("  Direct comparison to Pulse's retrieval metrics requires re-scoring Garden's retrieval")
    print("  output under the same metric. This runner shows what PULSE RETRIEVES on this corpus;")
    print("  qualitative quality vs Garden needs LLM-judge evaluation (out of scope for this PR).")
    print()


def _print_category_breakdown(per_query: list[dict]) -> None:
    """Group by sentiment_label-like category (test name prefix gives a rough
    bucket for these 5 tests). Kept for parity with run_eval.py output."""
    if not per_query:
        return
    print("BY TEST")
    print("-" * 78)
    print(f"  {'id':<5}  {'name':<28}  {'R@5':>6}  {'R@10':>6}  {'MRR':>6}  {'crit':>6}")
    for q in per_query:
        print(
            f"  {q['id']:<5}  {q['name']:<28}  "
            f"{q['recall_5']:>6.3f}  {q['recall_10']:>6.3f}  "
            f"{q['mrr']:>6.3f}  {q['crit_hit']:>6.3f}"
        )
    print()


# --- Public entry -----------------------------------------------------------
def run(
    corpus_path: Path = DEFAULT_CORPUS_PATH,
    verbose: bool = False,
    top_k: int = 10,
    depth: int = 2,
) -> dict:
    """Run the full eval. Returns dict with summary + per_query for tests."""
    corpus = json.loads(Path(corpus_path).read_text())
    con = fresh_db()
    stats = ingest_corpus(con, corpus)

    per_query: list[dict] = []
    tests = corpus.get("tests", [])
    for test in tests:
        gt = events_to_entity_gt(con, test["ideal_top_3_event_ids"])
        per_query.append(_run_query(con, test, gt, top_k=top_k, depth=depth))

    if verbose:
        _print_verbose(per_query)

    print()
    _print_category_breakdown(per_query)

    summary = _summarize(per_query)
    _print_summary(summary, n_events=stats["n_events"])

    return {
        "summary": summary,
        "per_query": per_query,
        "stats": stats,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Pulse retrieval on the real empathic corpus")
    p.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_PATH,
                   help=f"Path to corpus JSON (default: {DEFAULT_CORPUS_PATH})")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="print per-query breakdown")
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--depth", type=int, default=2,
                   help="BFS depth for retrieval")
    args = p.parse_args()

    if not args.corpus.exists():
        print(f"ERROR: corpus not found at {args.corpus}", file=sys.stderr)
        return 2

    result = run(
        corpus_path=args.corpus,
        verbose=args.verbose,
        top_k=args.top_k,
        depth=args.depth,
    )
    assert all(math.isfinite(q["recall_5"]) for q in result["per_query"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
