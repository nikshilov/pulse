"""Garden-comparable LLM-judge bench on empathic-memory-corpus.

This is Task C in the 2026-04-17 bench-hardening arc: produce a number
directly comparable to Garden's April bench result (Garden = 22.00/30 under
Opus-as-judge in empathic-memory-20260414).

Unlike `run_real_eval.py` which measures Recall@k / MRR (retrieval metrics),
this runner uses the SAME rubric judges used for Garden:

    Relevance (0-10) + Specificity (0-10) + Actionability (0-10) = /30

Reference numbers (from bench/results/empathic-memory-20260414-1914.md):
    Garden       : 24.05  (averaged across all 12 judges)
    Garden (Opus): 22.00  (Opus 4.6 as single judge)
    sqlite-vec   : 16.30
    Graphiti     : ~9
    MemPalace    : ~4

Flow per test:
  1. Run Pulse retrieve_context() → get top-k entities
  2. Pull their associated memories (event texts + facts linked to those entities)
  3. Take top-3 memories (highest-salience events from top-ranked entities)
  4. Build judge prompt using the corpus's test.fail_modes + ideal explanation
  5. Ask Claude Opus to score Pulse per rubric
  6. Parse JSON → {S01_rel, S01_spec, S01_act}

Aggregate: sum(rel+spec+act) / tests = /30 score.

Cost per run (5 tests, 1 system):
  ~3k input + ~200 output tokens × 5 tests × Opus ($15 in / $75 out / 1M)
  = ~$0.30/run. With --compare (2 systems): ~$0.60/run.

Usage:
    export OPENAI_API_KEY="..."     # for semantic mode
    export ANTHROPIC_API_KEY="..."  # for judge

    python scripts/bench/run_llm_judge.py                 # keyword only
    python scripts/bench/run_llm_judge.py --semantic      # hybrid only
    python scripts/bench/run_llm_judge.py --compare       # both, side-by-side
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from bench.run_real_eval import (  # noqa: E402
    DEFAULT_CORPUS_PATH,
    events_to_entity_gt,
    fresh_db,
    ingest_corpus,
)
from extract.intent import (  # noqa: E402
    classify_intent_llm,
    classify_intent_rules,
)
from extract.retrieval import retrieve_context  # noqa: E402
from pulse_consolidate import embed_entities  # noqa: E402


# Keywords used by rank_memories_by_intent's anchor_family filter. Mirror of
# _ANCHOR_FAMILY_PATTERNS but at memory-text level, where we can be simpler
# (substring rather than regex) and faster.
_FAMILY_TOKENS = (
    "family", "mom", "mum", "mother", "dad", "father", "parent",
    "brother", "sister", "sibling", "son", "daughter",
    "wife", "husband", "spouse", "fiancé", "fiancee", "fiance",
    "partner",
    "семь", "мам", "мать", "пап", "отец", "брат", "сестр",
    "сын", "дочь", "дочк", "жен", "муж", "родител",
)


JUDGE_MODEL = "claude-opus-4-6"
JUDGE_PROMPT_PATH = Path(
    os.path.expanduser("~/dev/ai/bench/prompts/judge-en.txt")
)


def _anthropic_client():
    """Lazy Anthropic client. Raises if key missing."""
    import anthropic  # deferred import
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY required for llm-judge runner")
    return anthropic.Anthropic(api_key=key)


def _pull_top_memories(con, entity_ids: list[int], corpus_events: list[dict],
                      max_memories: int = 3, intent: str = "cold_open") -> list[dict]:
    """Collect top-k memories for a list of retrieved entities.

    A "memory" here is the text of an event the entity participates in.
    We collect all candidate event-memories for the retrieved entities,
    then delegate to `rank_memories_by_intent` to choose ordering.
    """
    if not entity_ids:
        return []
    placeholders = ",".join("?" * len(entity_ids))
    rows = con.execute(
        f"SELECT DISTINCT e.id, e.title, e.description, e.sentiment "
        f"FROM events e "
        f"JOIN event_entities ee ON ee.event_id = e.id "
        f"WHERE ee.entity_id IN ({placeholders})",
        tuple(entity_ids),
    ).fetchall()
    # Match back to corpus for user_flag + days_ago
    by_id = {ev["id"]: ev for ev in corpus_events}
    pool: list[dict] = []
    for (eid, title, description, sentiment) in rows:
        cev = by_id.get(eid, {})
        pool.append({
            "id": eid,
            "text": description or title,
            "sentiment": sentiment or 0,
            "user_flag": 1 if cev.get("user_flag") else 0,
            "days_ago": cev.get("days_ago"),
        })
    ranked = rank_memories_by_intent(pool, intent)
    return ranked[:max_memories]


def _mentions_family(text: str) -> bool:
    """True if a memory's text references a family member / relationship."""
    if not text:
        return False
    low = text.lower()
    return any(tok in low for tok in _FAMILY_TOKENS)


def rank_memories_by_intent(memories: list[dict], intent: str) -> list[dict]:
    """Re-rank a pool of event-memories according to query intent.

    Each memory dict should have: id, text, sentiment, user_flag, days_ago.
    Returns the full pool sorted by intent-appropriate key. The caller
    slices to `max_memories`.

    Strategy table:

    | intent         | ordering                                               |
    |----------------|--------------------------------------------------------|
    | recent         | days_ago ASC, user_flag DESC                           |
    | weighs         | filter sentiment<0 → abs(sentiment) DESC, days_ago ASC;|
    |                | pad with remaining by abs(sentiment) DESC if <3        |
    | anchor_family  | filter text mentions family → user_flag DESC,          |
    |                | abs(sentiment) DESC; pad with user_flag=1 globals      |
    | opener         | user_flag DESC, abs(sentiment) DESC                    |
    | decoy_resist   | sentiment DESC, days_ago ASC (positives first);        |
    |                | preserve one user_flag as safety anchor in slot 2/3    |
    | cold_open      | user_flag DESC, abs(sentiment) DESC (baseline)         |
    """
    if not memories:
        return []

    def by_recent(m: dict) -> tuple:
        # Freshness-biased blend: fresher wins, but neutral mundane events
        # should not beat a slightly older emotionally-weighted event.
        # Primary key: days_ago penalized by emotional magnitude and flag.
        # A flagged item effectively gets a ~30-day freshness bonus; each
        # |sentiment| point gets a ~7-day bonus. This keeps "lately" queries
        # from surfacing neutral gym/tacos events over recent weddings.
        days = m.get("days_ago")
        if days is None:
            days = 10**9
        sent_mag = abs(m.get("sentiment", 0) or 0)
        flag = int(m.get("user_flag", 0))
        adjusted = days - 7 * sent_mag - 30 * flag
        return (adjusted, days)

    def by_weight(m: dict) -> tuple:
        return (-abs(m.get("sentiment", 0) or 0),
                m.get("days_ago") if m.get("days_ago") is not None else 10**9)

    def by_flag_then_weight(m: dict) -> tuple:
        return (-int(m.get("user_flag", 0)),
                -abs(m.get("sentiment", 0) or 0))

    if intent == "recent":
        return sorted(memories, key=by_recent)

    if intent == "weighs":
        negatives = [m for m in memories if (m.get("sentiment") or 0) < 0]
        others = [m for m in memories if (m.get("sentiment") or 0) >= 0]
        negatives_sorted = sorted(negatives, key=by_weight)
        others_sorted = sorted(others, key=by_weight)
        # Pad with non-negatives only if we have too few negatives for slot-3.
        if len(negatives_sorted) >= 3:
            return negatives_sorted + others_sorted
        return negatives_sorted + others_sorted

    if intent == "anchor_family":
        family = [m for m in memories if _mentions_family(m.get("text", ""))]
        other = [m for m in memories if not _mentions_family(m.get("text", ""))]
        family_sorted = sorted(family, key=by_flag_then_weight)
        # Pad with globally user-flagged entries not already in family set.
        family_ids = {m["id"] for m in family_sorted}
        padding_flagged = sorted(
            [m for m in other if m.get("user_flag") and m["id"] not in family_ids],
            key=by_flag_then_weight,
        )
        padding_rest = sorted(
            [m for m in other if not m.get("user_flag") and m["id"] not in family_ids],
            key=by_flag_then_weight,
        )
        return family_sorted + padding_flagged + padding_rest

    if intent == "opener":
        return sorted(memories, key=by_flag_then_weight)

    if intent == "decoy_resist":
        # Positive-first, recency-tiebreak. Then reserve one user_flag slot
        # as a safety-anchor (slot 2 if 2+ items, else append).
        positives = [m for m in memories if (m.get("sentiment") or 0) > 0]
        neutrals = [m for m in memories if (m.get("sentiment") or 0) == 0]
        negatives = [m for m in memories if (m.get("sentiment") or 0) < 0]

        def by_positive(m: dict) -> tuple:
            return (-1 * (m.get("sentiment") or 0),
                    m.get("days_ago") if m.get("days_ago") is not None
                    else 10**9)

        pos_sorted = sorted(positives, key=by_positive)
        neu_sorted = sorted(neutrals, key=by_positive)
        neg_sorted = sorted(
            negatives,
            key=lambda m: (-int(m.get("user_flag", 0)),
                           -abs(m.get("sentiment") or 0)),
        )

        base = pos_sorted + neu_sorted + neg_sorted
        # Promote the first user_flag grief anchor into slot 2 as a safety
        # warning — judges penalize surfacing grief top-1 but reward including
        # the anchor as context.
        anchor_idx = next(
            (i for i, m in enumerate(base)
             if m.get("user_flag") and (m.get("sentiment") or 0) < 0),
            None,
        )
        if anchor_idx is not None and anchor_idx > 1 and len(base) >= 2:
            anchor = base.pop(anchor_idx)
            base.insert(1, anchor)
        return base

    # cold_open — baseline behavior: user_flag first, then |sentiment|.
    return sorted(memories, key=by_flag_then_weight)


def _format_system_block(system_label: str, memories: list[dict]) -> str:
    """Render one system's top-3 memories into the judge-expected format."""
    lines = [f"{system_label}:"]
    if not memories:
        lines.append("  (no memories returned)")
        return "\n".join(lines)
    for i, m in enumerate(memories, 1):
        tag = "[FLAGGED] " if m.get("user_flag") else ""
        days = f" [{m['days_ago']}d ago]" if m.get("days_ago") is not None else ""
        lines.append(
            f"  Memory {i}: {tag}event_id={m['id']}{days} "
            f"sentiment={m['sentiment']:+.0f} — {m['text']}"
        )
    return "\n".join(lines)


def _build_judge_user_msg(test: dict, corpus_events: list[dict],
                          systems: list[tuple[str, list[dict]]]) -> str:
    """Assemble the judge's user message for one test query."""
    by_id = {ev["id"]: ev for ev in corpus_events}
    ideal_ids = test["ideal_top_3_event_ids"]
    ideal_block = "\n".join(
        f"  event {i}: {by_id.get(i, {}).get('text', '(missing)')}"
        for i in ideal_ids
    )
    fail_modes_block = "\n".join(f"  - {f}" for f in test.get("fail_modes", []))
    system_blocks = "\n\n".join(
        _format_system_block(label, mems) for (label, mems) in systems
    )

    # Emit only the slots we actually have so Opus doesn't hallucinate
    # scores for Systems 03-15. We pad with "(not run)" markers below.
    filled_slots = [label for (label, _) in systems]
    pad_lines = []
    for i in range(len(filled_slots) + 1, 16):
        pad_lines.append(f"System {i:02d}: (not run — score all dims as 0)")
    pad_block = "\n".join(pad_lines)

    return (
        f"## Conversation moment\n\n"
        f"User query: {test['user_query']!r}\n"
        f"What this tests: {test.get('what_it_tests', '(not specified)')}\n\n"
        f"## Ideal top-3 memories (event IDs: {ideal_ids})\n\n"
        f"{ideal_block}\n\n"
        f"Ideal explanation: {test.get('ideal_explanation', '')}\n\n"
        f"## Failure modes to penalize\n\n"
        f"{fail_modes_block}\n\n"
        f"## Retrieved memories per system\n\n"
        f"{system_blocks}\n\n"
        f"{pad_block}\n\n"
        f"## Scoring\n\n"
        f"Score each system on rel/spec/act (0-10 each). Return ONLY the JSON "
        f"object specified in the system prompt — no prose, no code fences."
    )


def _judge_one(client, judge_prompt: str, test: dict, corpus_events: list[dict],
               systems: list[tuple[str, list[dict]]]) -> dict:
    """Run one Opus judge call. Returns parsed JSON with per-system scores."""
    user_msg = _build_judge_user_msg(test, corpus_events, systems)
    resp = client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=1024,
        system=judge_prompt,
        messages=[{"role": "user", "content": user_msg}],
    )
    # Extract text from first content block
    text = ""
    for block in resp.content:
        if block.type == "text":
            text += block.text
    text = text.strip()
    # Tolerate optional fenced code block
    if text.startswith("```"):
        text = text.strip("`").split("\n", 1)[1].rsplit("\n", 1)[0]
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as ex:
        raise RuntimeError(f"judge returned non-JSON: {ex}\n---\n{text[:500]}")


def _retrieve_memories_for_test(con, test: dict, corpus_events: list[dict],
                                semantic: bool, embedder_model: str,
                                semantic_top_n: int,
                                intent: str = "cold_open") -> list[dict]:
    """Run Pulse retrieval for the test query, return top-3 event memories.

    The `intent` argument is forwarded to `_pull_top_memories`, which uses it
    to switch ranking strategy. Entity-level retrieval is unchanged.
    """
    result = retrieve_context(
        con, test["user_query"], top_k=10, depth=2,
        semantic=semantic, embedder_model=embedder_model,
        semantic_top_n=semantic_top_n,
    )
    entity_ids = [e["id"] for e in result["matched_entities"]]
    return _pull_top_memories(
        con, entity_ids, corpus_events, max_memories=3, intent=intent,
    )


def _get_intent_classifier(name: str):
    """Return a callable `(query) -> Intent` for the chosen backend.

    Supported backends:
      - "rules" (default): fast, deterministic, no API calls.
      - "llm": Claude Sonnet tool-use classifier; ~$0.001/query. Shares the
        same Anthropic client factory as the judge — if ANTHROPIC_API_KEY is
        missing, `classify_intent_llm` itself raises.
    """
    if name == "rules":
        return classify_intent_rules
    if name == "llm":
        return classify_intent_llm
    raise ValueError(
        f"unknown intent classifier {name!r}; expected 'rules' or 'llm'"
    )


def run(
    corpus_path: Path = DEFAULT_CORPUS_PATH,
    semantic: bool = False,
    embedder_model: str = "openai-text-embedding-3-large",
    semantic_top_n: int = 3,
    verbose: bool = False,
    intent_classifier: str = "rules",
) -> dict:
    corpus = json.loads(Path(corpus_path).read_text())
    con = fresh_db()
    ingest_corpus(con, corpus)
    if semantic:
        embed_entities(con, embedder_model=embedder_model, only_missing=True)

    judge_prompt = JUDGE_PROMPT_PATH.read_text()
    client = _anthropic_client()
    mode = "hybrid" if semantic else "keyword"
    system_label = f"System 01 (Pulse {mode}, intent={intent_classifier})"
    classify = _get_intent_classifier(intent_classifier)

    per_test: list[dict] = []
    for test in corpus["tests"]:
        intent = classify(test["user_query"])
        memories = _retrieve_memories_for_test(
            con, test, corpus["events"],
            semantic=semantic, embedder_model=embedder_model,
            semantic_top_n=semantic_top_n,
            intent=intent,
        )
        verdict = _judge_one(
            client, judge_prompt, test, corpus["events"],
            systems=[(system_label, memories)],
        )
        rel = verdict.get("S01_rel", 0)
        spec = verdict.get("S01_spec", 0)
        act = verdict.get("S01_act", 0)
        total = rel + spec + act
        per_test.append({
            "test_id": test["id"],
            "name": test["name"],
            "intent": intent,
            "rel": rel, "spec": spec, "act": act, "total": total,
            "note": verdict.get("note", ""),
            "memories": memories,
        })
        if verbose:
            print(f"[{test['id']}] {test['name']}  intent={intent}  "
                  f"rel={rel} spec={spec} act={act} total={total}/30")
            print(f"  note: {verdict.get('note', '')}")

    mean_total = sum(t["total"] for t in per_test) / len(per_test) if per_test else 0
    mean_rel = sum(t["rel"] for t in per_test) / len(per_test) if per_test else 0
    mean_spec = sum(t["spec"] for t in per_test) / len(per_test) if per_test else 0
    mean_act = sum(t["act"] for t in per_test) / len(per_test) if per_test else 0

    return {
        "mode": mode,
        "mean_total": mean_total,
        "mean_rel": mean_rel,
        "mean_spec": mean_spec,
        "mean_act": mean_act,
        "per_test": per_test,
    }


def _print_result(r: dict) -> None:
    print()
    print("=" * 78)
    print(f"PULSE ({r['mode']}) — LLM judge, Opus-4.6, empathic-memory-corpus")
    print("=" * 78)
    print(f"  Relevance      : {r['mean_rel']:.2f} / 10")
    print(f"  Specificity    : {r['mean_spec']:.2f} / 10")
    print(f"  Actionability  : {r['mean_act']:.2f} / 10")
    print(f"  TOTAL          : {r['mean_total']:.2f} / 30")
    print()
    print("Reference (empathic-memory-20260414 bench, same rubric, Opus judge):")
    print("  Garden         : 22.00 / 30")
    print("  sqlite-vec     : ~15.00 / 30")
    print("  Graphiti       : ~9.00 / 30")
    print("  MemPalace      : ~4.00 / 30")
    print()


def main() -> int:
    p = argparse.ArgumentParser(
        description="Pulse retrieval scored by Claude Opus, Garden-comparable."
    )
    p.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_PATH)
    p.add_argument("--semantic", action="store_true",
                   help="enable hybrid retrieval (keyword + OpenAI embeddings)")
    p.add_argument("--embedder-model", default="openai-text-embedding-3-large",
                   choices=["fake-local", "openai-text-embedding-3-large"])
    p.add_argument("--semantic-top-n", type=int, default=3)
    p.add_argument("--compare", action="store_true",
                   help="run both keyword and hybrid, print side-by-side")
    p.add_argument(
        "--intent-classifier",
        default="rules",
        choices=["rules", "llm"],
        help=(
            "which intent classifier to use per test query. "
            "'rules' (default): fast, deterministic, zero API cost. "
            "'llm': Claude Sonnet tool-use, ~$0.001/query — safety net for "
            "queries the rules miss (indirect phrasing, irony, mixed lang)."
        ),
    )
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    if not args.corpus.exists():
        print(f"ERROR: corpus not found at {args.corpus}", file=sys.stderr)
        return 2

    if args.compare:
        print(">>> KEYWORD")
        kw = run(
            corpus_path=args.corpus,
            semantic=False,
            verbose=args.verbose,
            intent_classifier=args.intent_classifier,
        )
        _print_result(kw)
        print(">>> HYBRID")
        hy = run(
            corpus_path=args.corpus,
            semantic=True,
            embedder_model=args.embedder_model,
            semantic_top_n=args.semantic_top_n,
            verbose=args.verbose,
            intent_classifier=args.intent_classifier,
        )
        _print_result(hy)
        print("=" * 78)
        print("DELTA:  hybrid − keyword")
        print("=" * 78)
        print(f"  Rel  : {hy['mean_rel']:.2f} − {kw['mean_rel']:.2f} "
              f"= {hy['mean_rel'] - kw['mean_rel']:+.2f}")
        print(f"  Spec : {hy['mean_spec']:.2f} − {kw['mean_spec']:.2f} "
              f"= {hy['mean_spec'] - kw['mean_spec']:+.2f}")
        print(f"  Act  : {hy['mean_act']:.2f} − {kw['mean_act']:.2f} "
              f"= {hy['mean_act'] - kw['mean_act']:+.2f}")
        print(f"  TOTAL: {hy['mean_total']:.2f} − {kw['mean_total']:.2f} "
              f"= {hy['mean_total'] - kw['mean_total']:+.2f}")
        print()
        return 0

    r = run(
        corpus_path=args.corpus,
        semantic=args.semantic,
        embedder_model=args.embedder_model,
        semantic_top_n=args.semantic_top_n,
        verbose=args.verbose,
        intent_classifier=args.intent_classifier,
    )
    _print_result(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
