"""Event-level semantic retrieval — Pulse v2_pure.

The winning config from the 2026-04-18 bench sweep on Nik's real corpus
(47 empathic+mixed queries, 3-judge panel): **28.71 ± 1.40** vs Mem0 21.75
(Δ +6.96, wins on all 3 judges). See
`scripts/bench/baselines/EMPATHIC_SUBSET_RESULTS.md`.

## Why this exists alongside extract/retrieval.py

`retrieval.py` is entity-level: keyword-BFS matches canonical entity names,
expands via relations, ranks entities. It works well on Alex-style labeled
corpora (named characters, short queries) but fails on Nik-style
conversational bursts — 98% of Russian queries found zero canonical-name
matches and fell through to a static salience fallback (same 3 wound-anchors
on every query). Diagnosed & documented in
`memory/project_pulse_mem0_loss_2026_04_17.md`.

`retrieval_v2.py` is **event-level**: embed each event's full text at ingest,
embed the query at retrieval time, cosine similarity, light recency decay.
No sentiment amplifier. No anchor boost. No emotional_weight multiplier.
No entity matcher. On Nik's real corpus this config beats Mem0 by ~7 points
on a 3-judge panel.

The two are complementary, not replacements:
- `retrieval.py` returns ENTITIES (with BFS-expanded relations & facts)
  — use when you need "who/what is this person/thing" context.
- `retrieval_v2.py` returns EVENTS — use when you need "what happened /
  what did Nik say / what moment is this like" context.

A production Pulse harness should call both and merge. This module is
the event path.

## The formula

    score(query, event) = cosine(query_vec, event_vec)
                         × exp(-λ · days_ago)

Winning params (from sweep):
    λ = 0.001   # half-life ~700 days, gentle — preserves older events
                # λ = 0.003 (half-life 230d) was the v2 default; too aggressive
                # on Nik's corpus, suppressed legitimately old but still-relevant
                # wound/joy anchors.

Deliberately NOT in the default:
    - `(1 + α·|sentiment|)` multiplier — any α > 0 dropped score by 9+
      points on the bench. Full text via embedding already captures emotional
      salience; an extra multiplier just over-boosts high-magnitude events
      regardless of topic match.
    - `anchor = 1.5 if user_flag else 1.0` — similar issue. Wound-anchors
      flood top-3 on unrelated queries.
    - Intent classifier rerank — adds latency and cost for no measurable
      judge-score gain at this scale.

A conservative conditional boost for typed-emotion alignment is planned for
v3 (typed emotional signatures) but is NOT part of v2_pure.

## API stability

`retrieve_events()` output shape is a list[dict] each containing:
    id, text, sentiment, emotional_weight, days_ago, cosine, score

Extra fields may be added; never removed. `score` may be renormalised in
future versions but `cosine` stays a raw cosine similarity in [-1, 1].
`user_flag` is NOT included because the production events table doesn't
carry that column — it exists only in bench fixtures. Bench runners that
need it can remap from their own corpus metadata.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone

from extract.embedder import embed_texts


# Light recency decay. Half-life = ln(2) / λ ≈ 693 days at λ=0.001.
# Bench sweep (2026-04-18) picked this over 0.003 (which gave half-life 231d
# and lost ~1 point to harder events). See
# `scripts/bench/baselines/EMPATHIC_SUBSET_RESULTS.md`.
DEFAULT_LAMBDA = 0.001

# Per-belief-class decay rates (migration 014).
# Applied at retrieval time: effective_lambda = BELIEF_DECAY[belief_class].
# Events without belief_class (pre-014 rows) fall back to DEFAULT_LAMBDA.
#
# Rationale per class:
#   axiom       — permanent truths, never decay (Nik's core-wound, Elle's identity)
#   self_model  — slow (Elle's introspective facts — slow evolution)
#   user_model  — default (Nik's psychological profile — long-lived but not eternal)
#   operational — faster (day-to-day context, preferences)
#   hypothesis  — fastest (provisional reads awaiting confirmation)
BELIEF_DECAY: dict[str, float] = {
    "axiom":       0.0,
    "self_model":  0.0005,
    "user_model":  0.001,
    "operational": 0.003,
    "hypothesis":  0.005,
}

# Embedder default. Fake-local exists for unit tests; production callers
# should pass 'openai-text-embedding-3-large' explicitly so the choice is
# visible in logs and the OpenAI API key presence is checked at call time.
DEFAULT_EMBEDDER = "fake-local"

# How many events we consider as candidates before returning the top-k.
# Bench showed top_n=20 is the empirical sweet spot on 85-event Nik corpus;
# larger pools don't help because low-cosine events at rank 30+ rarely have
# anything to add and the recency multiplier alone can't rescue them.
# Raise this on larger corpora (> 500 events).
DEFAULT_TOP_N_CANDIDATES = 20


def retrieve_events(
    con: sqlite3.Connection,
    query: str,
    *,
    top_k: int = 3,
    lam: float = DEFAULT_LAMBDA,
    embedder_model: str = DEFAULT_EMBEDDER,
    top_n_candidates: int = DEFAULT_TOP_N_CANDIDATES,
    use_belief_class: bool = True,
) -> list[dict]:
    """Return top-k events by (cosine × recency) rank.

    Requires `event_embeddings` rows for the events you care about — call
    `embed_events()` after ingest to backfill. Events with no embedding are
    invisible to this retriever (they can't be ranked without a vector).

    Args:
        con: open SQLite connection with migrations applied up to 013.
        query: user message to retrieve memories for.
        top_k: number of events to return. Default 3.
        lam: recency decay rate per day. 0.001 = gentle (half-life ~700d),
            matches the bench winner. Set to 0.0 to disable recency entirely.
        embedder_model: which embedder to use for the QUERY. Must match the
            model used to populate `event_embeddings`; otherwise cosine is
            nonsense. Default 'fake-local' is test-only — production callers
            pass 'openai-text-embedding-3-large' explicitly.
        top_n_candidates: pool size before recency reranking. Default 20.

    Returns:
        List of dicts (up to top_k), each with:
            id, text, sentiment, emotional_weight, days_ago, cosine, score
        Sorted by score descending.

    Raises:
        sqlite3.OperationalError: if `event_embeddings` table is missing
            (migration 013 not applied).
    """
    # Fetch all embeddings filtered to the requested model. Cross-model
    # cosine is meaningless — silently skipping is worse than empty result,
    # so we filter explicitly.
    rows = con.execute(
        "SELECT event_id, vector_json FROM event_embeddings WHERE model = ?",
        (embedder_model,),
    ).fetchall()
    if not rows:
        return []

    query_vec = embed_texts([query], model=embedder_model)[0]

    scored: list[tuple[float, float, int]] = []  # (cosine, score, event_id)
    for event_id, vector_json in rows:
        try:
            vec = json.loads(vector_json)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(vec, list) or not vec:
            continue
        cos = _cosine(query_vec, vec)
        if cos <= 0.0:
            # Negative/zero cosines are noise at this scale; skip before
            # paying for a DB round-trip.
            continue
        scored.append((cos, cos, event_id))

    # Keep top_n_candidates by raw cosine, then rerank with recency
    scored.sort(key=lambda t: t[0], reverse=True)
    candidates = scored[:top_n_candidates]

    now = datetime.now(timezone.utc)
    ranked: list[tuple[float, dict]] = []
    for cos, _, event_id in candidates:
        event = _fetch_event(con, event_id)
        if event is None:
            continue
        days_ago = _days_since(event.get("ts"), now)
        if days_ago is None:
            days_ago = 30  # Unknown timestamps: treat as mildly recent.
        # Pick decay rate: per-belief-class (post-014) or caller-supplied uniform.
        if use_belief_class:
            effective_lam = BELIEF_DECAY.get(event.get("belief_class", "operational"), lam)
        else:
            effective_lam = lam
        recency = math.exp(-effective_lam * days_ago)
        # confidence_floor: minimum score floor (axiom-preservation mechanic).
        # A core-wound belief with floor=0.85 survives 10-year-old recency.
        # We apply floor to the recency × cosine product: the belief cannot
        # lose salience below (floor × cosine). Pre-014 rows have floor=0 → no-op.
        floor = event.get("confidence_floor", 0.0) or 0.0
        base = cos * recency
        score = max(base, cos * floor) if floor > 0 else base
        event["cosine"] = cos
        event["score"] = score
        event["days_ago"] = days_ago
        event["effective_lambda"] = effective_lam
        ranked.append((score, event))

    ranked.sort(key=lambda t: t[0], reverse=True)
    return [event for _, event in ranked[:top_k]]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cosine(a: list[float], b: list[float]) -> float:
    """Pure-Python cosine similarity. Zero deps, zero numpy."""
    if len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _fetch_event(con: sqlite3.Connection, event_id: int) -> dict | None:
    """Load one event's retrieval-relevant fields. None if missing.

    Migration 014 added belief_class, confidence_floor, archivable, provenance.
    This helper reads them via COALESCE so pre-014 databases (no columns)
    still work — the PRAGMA table_info dance is avoided by catching the
    OperationalError path via a defensive try/except over the richer query.
    """
    # Try the richer query first (post-migration-014).
    try:
        row = con.execute(
            "SELECT id, title, description, sentiment, emotional_weight, ts, "
            "       COALESCE(belief_class, 'operational') AS belief_class, "
            "       COALESCE(confidence_floor, 0.0) AS confidence_floor, "
            "       COALESCE(archivable, 1) AS archivable, "
            "       COALESCE(provenance, 'interactive_memory') AS provenance "
            "FROM events WHERE id = ?",
            (event_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        # Pre-014 DB — columns don't exist. Fall back.
        row = con.execute(
            "SELECT id, title, description, sentiment, emotional_weight, ts "
            "FROM events WHERE id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            return None
        eid, title, description, sentiment, emotional_weight, ts = row
        text = description or title or ""
        return {
            "id": eid, "text": text,
            "sentiment": sentiment or 0.0,
            "emotional_weight": emotional_weight or 0.0, "ts": ts,
            "belief_class": "operational",
            "confidence_floor": 0.0,
            "archivable": 1,
            "provenance": "interactive_memory",
        }
    if row is None:
        return None
    (eid, title, description, sentiment, emotional_weight, ts,
     belief_class, confidence_floor, archivable, provenance) = row
    text = description or title or ""
    return {
        "id": eid,
        "text": text,
        "sentiment": sentiment or 0.0,
        "emotional_weight": emotional_weight or 0.0,
        "ts": ts,
        "belief_class": belief_class,
        "confidence_floor": confidence_floor,
        "archivable": int(archivable),
        "provenance": provenance,
    }


def _days_since(ts: str | None, now: datetime) -> int | None:
    """Days from ISO-8601 ts to now. None on any parse failure.

    Tolerant of missing/garbage timestamps — callers treat None as "unknown"
    and fall back to a neutral days_ago default.
    """
    if not ts:
        return None
    try:
        last = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    delta = (now - last).days
    return max(0, delta)


# ---------------------------------------------------------------------------
# Ingest-side: backfill event_embeddings
# ---------------------------------------------------------------------------

def embed_events(
    con: sqlite3.Connection,
    *,
    embedder_model: str = DEFAULT_EMBEDDER,
    only_missing: bool = True,
    batch_size: int = 50,
) -> int:
    """Embed event texts into `event_embeddings`. Returns count written.

    Args:
        con: open SQLite connection with migration 013 applied.
        embedder_model: which embedder backend. 'fake-local' for tests,
            'openai-text-embedding-3-large' for production.
        only_missing: if True, skip events that already have an embedding
            for this model. Default True — idempotent re-runs are free.
            Pass False to force re-embedding (e.g. after prompt/model change).
        batch_size: OpenAI batch size. 50 is safe for the 8191-token input
            limit at typical event text lengths.

    Returns:
        Number of (event_id, model) rows written.

    Raises:
        sqlite3.OperationalError: if `event_embeddings` table is missing.
    """
    if only_missing:
        rows = con.execute(
            "SELECT e.id, COALESCE(e.description, e.title, '') "
            "FROM events e "
            "LEFT JOIN event_embeddings ee "
            "  ON ee.event_id = e.id AND ee.model = ? "
            "WHERE ee.event_id IS NULL "
            "  AND COALESCE(e.description, e.title, '') != ''",
            (embedder_model,),
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT id, COALESCE(description, title, '') FROM events "
            "WHERE COALESCE(description, title, '') != ''"
        ).fetchall()

    if not rows:
        return 0

    written = 0
    for i in range(0, len(rows), batch_size):
        chunk = rows[i:i + batch_size]
        event_ids = [r[0] for r in chunk]
        texts = [r[1] for r in chunk]
        vecs = embed_texts(texts, model=embedder_model)
        if len(vecs) != len(texts):
            raise RuntimeError(
                f"embedder returned {len(vecs)} vectors for {len(texts)} texts"
            )
        for event_id, text, vec in zip(event_ids, texts, vecs):
            con.execute(
                "INSERT OR REPLACE INTO event_embeddings "
                "(event_id, model, dim, vector_json, text_source, updated_at) "
                "VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
                (event_id, embedder_model, len(vec), json.dumps(vec), text),
            )
            written += 1
    con.commit()
    return written
