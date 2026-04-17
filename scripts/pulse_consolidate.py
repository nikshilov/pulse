#!/usr/bin/env python3
"""Pulse graph consolidation: dedup entities, close stale questions, execute merges, report stats."""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from scripts.elle_feel.models import HrvPoint


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
        sp = f"merge_{proposal_id}"
        con.execute(f"SAVEPOINT {sp}")
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
                try:
                    merged_aliases.update(json.loads(from_row[1]))
                except (json.JSONDecodeError, TypeError):
                    pass
            if to_row and to_row[0]:
                try:
                    merged_aliases.update(json.loads(to_row[0]))
                except (json.JSONDecodeError, TypeError):
                    pass
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
            con.execute(f"RELEASE {sp}")
            merged += 1
        except Exception:
            con.execute(f"ROLLBACK TO {sp}")
            con.execute(f"RELEASE {sp}")
            raise

    return merged


# ---------------------------------------------------------------------------
# Task 3: Skip-guard
# ---------------------------------------------------------------------------

def _get_metadata(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("SELECT value FROM consolidation_metadata WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _set_metadata(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO consolidation_metadata (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, datetime.now(timezone.utc).isoformat()),
    )


def _should_skip(con: sqlite3.Connection) -> dict | None:
    last_ts = _get_metadata(con, "last_consolidation_ts")
    if not last_ts:
        return None
    new_entities = con.execute(
        "SELECT COUNT(*) FROM entities WHERE first_seen > ?", (last_ts,)
    ).fetchone()[0]
    new_evidence = con.execute(
        "SELECT COUNT(*) FROM evidence WHERE created_at > ?", (last_ts,)
    ).fetchone()[0]
    new_events = con.execute(
        "SELECT COUNT(*) FROM events WHERE ts > ?", (last_ts,)
    ).fetchone()[0]
    if new_entities == 0 and new_evidence < 3 and new_events == 0:
        return {"skipped": True, "reason": "no significant changes since last consolidation"}
    return None


# ---------------------------------------------------------------------------
# Task 4: Co-occurrence Detection
# ---------------------------------------------------------------------------

def find_cooccurrence_candidates(con: sqlite3.Connection, min_cooccurrences: int = 3) -> list[dict]:
    rows = con.execute(
        "SELECT ee1.entity_id AS a, ee2.entity_id AS b, COUNT(*) AS co_count "
        "FROM event_entities ee1 "
        "JOIN event_entities ee2 ON ee1.event_id = ee2.event_id "
        "    AND ee1.entity_id < ee2.entity_id "
        "GROUP BY ee1.entity_id, ee2.entity_id "
        "HAVING co_count >= ?",
        (min_cooccurrences,),
    ).fetchall()
    candidates = []
    for a_id, b_id, count in rows:
        existing = con.execute(
            "SELECT COUNT(*) FROM relations "
            "WHERE (from_entity_id=? AND to_entity_id=?) OR (from_entity_id=? AND to_entity_id=?)",
            (a_id, b_id, b_id, a_id),
        ).fetchone()[0]
        if existing == 0:
            a_name = con.execute("SELECT canonical_name FROM entities WHERE id=?", (a_id,)).fetchone()[0]
            b_name = con.execute("SELECT canonical_name FROM entities WHERE id=?", (b_id,)).fetchone()[0]
            candidates.append({
                "entity_a_id": a_id, "entity_a_name": a_name,
                "entity_b_id": b_id, "entity_b_name": b_name,
                "co_count": count,
            })
    return candidates


# ---------------------------------------------------------------------------
# Task 5: Knowledge Gaps + Auto-populate open_questions
# ---------------------------------------------------------------------------

def detect_knowledge_gaps(con: sqlite3.Connection, min_mentions: int = 3, max_facts: int = 1) -> list[dict]:
    """Find entities the system has seen often but barely understands.

    Safety: rows with `do_not_probe = 1` are excluded at SQL level (structural opt-out,
    user-controllable). `emotional_weight` is returned so `auto_populate_questions`
    can apply a second, emotion-based gate before actually asking anything.
    """
    rows = con.execute(
        "SELECT e.id, e.canonical_name, e.kind, e.emotional_weight, "
        "       COUNT(DISTINCT ee.event_id) AS mention_count, "
        "       COUNT(DISTINCT f.id) AS fact_count "
        "FROM entities e "
        "LEFT JOIN event_entities ee ON ee.entity_id = e.id "
        "LEFT JOIN facts f ON f.entity_id = e.id "
        "WHERE e.do_not_probe = 0 "
        "GROUP BY e.id "
        "HAVING mention_count > ? AND fact_count <= ? "
        "ORDER BY mention_count DESC LIMIT 10",
        (min_mentions, max_facts),
    ).fetchall()
    return [
        {"entity_id": r[0], "canonical_name": r[1], "kind": r[2],
         "emotional_weight": r[3] or 0.0,
         "mention_count": r[4], "fact_count": r[5]}
        for r in rows
    ]


def auto_populate_questions(con: sqlite3.Connection, gaps: list[dict], ttl_days: int = 30) -> int:
    """Insert auto-generated "what's up with X?" open_questions for knowledge gaps.

    Safety gate (in addition to the structural `do_not_probe` filter upstream):
    any gap whose `emotional_weight > 0.6` is skipped silently. Elle does not
    ask about emotionally heavy entities (wounds, ex-partners, trauma threads)
    on her own — those require the human to open the door.
    """
    now = datetime.now(timezone.utc)
    ttl = (now + timedelta(days=ttl_days)).isoformat()
    now_iso = now.isoformat()
    added = 0
    for gap in gaps:
        if (gap.get("emotional_weight") or 0.0) > 0.6:
            continue
        result = con.execute(
            "INSERT OR IGNORE INTO open_questions "
            "(subject_entity_id, question_text, asked_at, ttl_expires_at, state) "
            "VALUES (?, ?, ?, ?, 'open')",
            (gap["entity_id"], f"Что сейчас с {gap['canonical_name']}?", now_iso, ttl),
        )
        if result.rowcount > 0:
            added += 1
    return added


# ---------------------------------------------------------------------------
# Task 7: Observability Metrics
# ---------------------------------------------------------------------------

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
        return {"trend": "insufficient", "days": days, "data_points": len(sentiments)}
    recent_avg = sum(sentiments[-7:]) / len(sentiments[-7:]) if len(sentiments) >= 7 else sentiments[-1]
    overall_avg = sum(sentiments) / len(sentiments)
    return {
        "trend": "improving" if recent_avg > overall_avg + 0.1 else "declining" if recent_avg < overall_avg - 0.1 else "stable",
        "recent_7d_avg": round(recent_avg, 3),
        "overall_avg": round(overall_avg, 3),
        "days": days,
        "data_points": len(sentiments),
    }


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


# ---------------------------------------------------------------------------
# Embedding generation (Phase-3 POC — semantic retrieval side channel)
# ---------------------------------------------------------------------------

def embed_entities(
    con: sqlite3.Connection,
    embedder_model: str = "fake-local",
    only_missing: bool = True,
    batch_size: int = 50,
) -> int:
    """Generate / refresh entity embeddings and UPSERT into `entity_embeddings`.

    Selection rule:
      - if `only_missing=True` (default): pick entities that either have no
        embedding row OR whose `last_seen` is newer than
        `entity_embeddings.updated_at` (need re-embed because the entity was
        touched after it was last embedded).
      - if `only_missing=False`: pick ALL entities — forces a full re-embed,
        useful when switching models.

    Entities with `do_not_probe=1` are NOT excluded here — they still get
    embedded so that downstream consolidation logic can use them if it ever
    lifts the gate. The retrieval path is what enforces the opt-out (seed-
    level and BFS-level gates inside `retrieval.py`).

    text_source format (one-line concatenation of the entity's retrieval-
    relevant text):
        "{canonical_name} | kind={kind} | aliases={csv} | {top 3 fact texts}"

    Batching: `batch_size` texts per `embed_texts` call. For `fake-local` the
    batch size is largely irrelevant (it's just SHA-256), but for OpenAI it
    bounds the per-request payload. 50 is a safe default at the OpenAI
    text-embedding-3-large input-tokens-per-request limit.

    Returns the count of rows upserted.
    """
    # Import path depends on how the caller entered the module:
    #   - pytest / CLI with `scripts/` on sys.path → `extract.embedder`
    #   - external caller with the repo root on sys.path → `scripts.extract.embedder`
    try:
        from extract.embedder import embed_texts, embedding_dim
    except ImportError:
        from scripts.extract.embedder import embed_texts, embedding_dim  # type: ignore

    if only_missing:
        rows = con.execute(
            "SELECT e.id, e.canonical_name, e.kind, e.aliases, e.last_seen "
            "FROM entities e "
            "LEFT JOIN entity_embeddings ee ON ee.entity_id = e.id "
            "WHERE ee.entity_id IS NULL OR e.last_seen > ee.updated_at"
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT id, canonical_name, kind, aliases, last_seen FROM entities"
        ).fetchall()

    if not rows:
        return 0

    # Build (entity_id, text_source) pairs. Pulling the top 3 facts per entity
    # is a separate cheap query per row; at POC scale this is fine.
    records: list[tuple[int, str]] = []
    for eid, name, kind, aliases_json, _last_seen in rows:
        try:
            aliases = json.loads(aliases_json) if aliases_json else []
        except (json.JSONDecodeError, TypeError):
            aliases = []
        fact_rows = con.execute(
            "SELECT text FROM facts WHERE entity_id = ? "
            "ORDER BY confidence DESC, id ASC LIMIT 3",
            (eid,),
        ).fetchall()
        fact_texts = [r[0] for r in fact_rows if r[0]]
        text_source = (
            f"{name} | kind={kind or 'unknown'} | "
            f"aliases={', '.join(aliases)} | {' '.join(fact_texts)}"
        ).strip()
        records.append((eid, text_source))

    dim = embedding_dim(embedder_model)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    upserted = 0

    for i in range(0, len(records), batch_size):
        chunk = records[i : i + batch_size]
        texts = [t for (_, t) in chunk]
        vectors = embed_texts(texts, model=embedder_model)
        for (eid, text_source), vec in zip(chunk, vectors):
            con.execute(
                "INSERT INTO entity_embeddings "
                "(entity_id, model, dim, vector_json, text_source, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(entity_id) DO UPDATE SET "
                "    model=excluded.model, "
                "    dim=excluded.dim, "
                "    vector_json=excluded.vector_json, "
                "    text_source=excluded.text_source, "
                "    updated_at=excluded.updated_at",
                (eid, embedder_model, dim, json.dumps(vec), text_source, now),
            )
            upserted += 1

    return upserted


# ---------------------------------------------------------------------------
# Main consolidation runner
# ---------------------------------------------------------------------------

def run_consolidation(
    db_path: str,
    hrv_points: "Sequence[HrvPoint] | None" = None,
    self_entity_id: int | None = None,
    embed_model: str | None = None,
) -> dict:
    """Run the full consolidation pipeline.

    Decay model: as of the Garden-style retrieval scoring shipped in PR #5,
    salience decay is *retrieval-time only* (see `scripts/extract/retrieval.py:_rank`,
    where `recency = exp(-λ × days)` is applied on read). The previous
    mutation-based `decay_salience` function — which irreversibly erosed
    `salience_score` in the DB on every cron tick — has been removed. That design
    was non-idempotent (double-firing cron = double erosion) and destroyed signal
    that the retrieval pass can reconstruct non-destructively from `last_seen`.

    What remains here is dedup, stale-question closing, approved merges,
    co-occurrence candidates, knowledge-gap auto-questions (with safety gates),
    observability metrics, and (optionally) two care-message emission paths —
    HRV trend and valence trend — that enqueue gentle Elle-side check-ins into
    `open_questions` when signals warrant.

    Optional params:
      hrv_points: list of daily HRV readings. If None (default), the HRV
                  care-message path is skipped entirely. Opt-in until Nik
                  connects real Apple Health data on VDS. Kept optional so
                  existing tests and CLI callers remain source-compatible.
      self_entity_id: if provided, care messages are enqueued with that
                      subject_entity_id. If None (default), the row is
                      enqueued with NULL subject — the question is still
                      addressable by its text and the VDS worker can route
                      it directly to Nik.
      embed_model: if set (e.g. "fake-local" or
                   "openai-text-embedding-3-large"), run `embed_entities`
                   after decay/dedup/co-occurrence so any newly-discovered
                   entities from this consolidation tick get embedded.
                   Default None = skip embedding entirely (zero behaviour
                   change versus pre-Phase-3 consolidation).
    """
    con = _open_connection(db_path)

    try:
        # Task 3: skip-guard
        skip = _should_skip(con)
        if skip:
            _set_metadata(con, "last_consolidation_ts", datetime.now(timezone.utc).isoformat())
            return skip

        # Read-only stats (no tx needed)
        stats = entity_stats(con)
        duplicates = find_duplicate_candidates(con)

        # Task 4: co-occurrence (read-only)
        cooccurrences = find_cooccurrence_candidates(con)

        # Task 5: knowledge gaps (read-only)
        gaps = detect_knowledge_gaps(con)

        # Task 7: observability (read-only)
        trend = valence_trend(con)
        efficiency = extraction_efficiency(con)

        # Care-message emission (opt-in; defaults skip both paths).
        hrv_result: dict = {"enqueued": False, "signal_kind": "skipped",
                            "tone": None, "question_id": None}
        valence_result: dict = {"enqueued": False, "reason": "skipped",
                                "question_id": None}

        # Mutating operations in a single transaction — anything that
        # writes goes in here so either everything commits or nothing does.
        con.execute("BEGIN IMMEDIATE")
        try:
            closed = close_stale_questions(con)
            merges = process_approved_merges(con)
            questions_added = auto_populate_questions(con, gaps)

            # HRV care-message path: only if caller supplied points.
            if hrv_points is not None:
                # Import inside the function so running consolidation
                # without the elle_feel package importable (edge case)
                # doesn't blow up at module-load time.
                from scripts.elle_feel.integration import check_and_enqueue
                hrv_result = check_and_enqueue(
                    con, list(hrv_points), self_entity_id=self_entity_id
                )

            # Valence care-message path: always-on, but gated on signal
            # quality. Declining trend + >=7 days of data => one message.
            if trend.get("trend") == "declining" and int(trend.get("data_points", 0)) >= 7:
                from scripts.elle_feel.valence_message import generate_valence_message
                text = generate_valence_message(trend)
                if text is not None:
                    now = datetime.now(timezone.utc)
                    now_iso = now.isoformat()
                    ttl_iso = (now + timedelta(days=3)).isoformat()
                    cur = con.execute(
                        "INSERT OR IGNORE INTO open_questions "
                        "(subject_entity_id, question_text, asked_at, ttl_expires_at, state) "
                        "VALUES (?, ?, ?, ?, 'open')",
                        (self_entity_id, text, now_iso, ttl_iso),
                    )
                    if cur.rowcount > 0:
                        valence_result = {"enqueued": True, "reason": "declining_trend",
                                          "question_id": cur.lastrowid}
                    else:
                        valence_result = {"enqueued": False, "reason": "dedup_hit",
                                          "question_id": None}

            # Embedding generation (Phase-3 POC). Opt-in only — default
            # embed_model=None skips entirely. Runs LATE so any entities
            # touched/created above (merges redirecting facts, new rows
            # from upstream extract jobs since last consolidate) get
            # refreshed vectors in the same transaction.
            embeddings_upserted = 0
            if embed_model is not None:
                embeddings_upserted = embed_entities(
                    con, embedder_model=embed_model, only_missing=True
                )

            _set_metadata(con, "last_consolidation_ts", datetime.now(timezone.utc).isoformat())
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()

    return {
        "stats": stats,
        "duplicate_candidates": len(duplicates),
        "duplicates": duplicates[:20],
        "stale_questions_closed": closed,
        "merges_executed": merges,
        "implicit_relation_candidates": cooccurrences,
        "knowledge_gaps": len(gaps),
        "questions_auto_added": questions_added,
        "valence_trend": trend,
        "extraction_efficiency": efficiency,
        "hrv_care": hrv_result,
        "valence_care": valence_result,
        "embeddings_upserted": embeddings_upserted,
        "embed_model": embed_model,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Pulse graph consolidation")
    parser.add_argument("--db", required=True, help="Path to pulse database")
    args = parser.parse_args()

    report = run_consolidation(args.db)

    if report.get("skipped"):
        print(f"=== Consolidation SKIPPED: {report['reason']} ===")
        return 0

    print("=== Consolidation Report ===")
    print(f"Entities: {report['stats']['total_entities']} ({report['stats']['entities_by_kind']})")
    print(f"Orphans: {report['stats']['orphan_entities']}")
    print(f"Relations: {report['stats']['total_relations']}")
    print(f"Facts: {report['stats']['total_facts']}")
    print(f"Duplicate candidates: {report['duplicate_candidates']}")
    for dup in report["duplicates"]:
        print(f"  {dup['entity_a_name']} <-> {dup['entity_b_name']} ({dup['similarity']})")
    print(f"Implicit relation candidates: {len(report.get('implicit_relation_candidates', []))}")
    for irc in report.get("implicit_relation_candidates", [])[:5]:
        print(f"  {irc['entity_a_name']} <-> {irc['entity_b_name']} (co-occurs {irc['co_count']}x)")
    print(f"Knowledge gaps: {report.get('knowledge_gaps', 0)}")
    print(f"Questions auto-added: {report.get('questions_auto_added', 0)}")
    print(f"Stale questions closed: {report['stale_questions_closed']}")
    print(f"Merges executed: {report['merges_executed']}")
    vt = report.get("valence_trend", {})
    print(f"Valence trend: {vt.get('trend', 'N/A')} (recent={vt.get('recent_7d_avg', 'N/A')}, overall={vt.get('overall_avg', 'N/A')}, points={vt.get('data_points', 0)})")
    ee = report.get("extraction_efficiency", {})
    print(f"Extraction efficiency: {ee.get('entities_per_1k_tokens', 0)} entities/1K tokens ({ee.get('total_entities', 0)} entities, {ee.get('total_tokens', 0)} tokens)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
