#!/usr/bin/env python3
"""Pulse graph consolidation: dedup entities, close stale questions, execute merges, report stats."""

import argparse
import json
import math
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
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
    rows = con.execute(
        "SELECT e.id, e.canonical_name, e.kind, "
        "       COUNT(DISTINCT ee.event_id) AS mention_count, "
        "       COUNT(DISTINCT f.id) AS fact_count "
        "FROM entities e "
        "LEFT JOIN event_entities ee ON ee.entity_id = e.id "
        "LEFT JOIN facts f ON f.entity_id = e.id "
        "GROUP BY e.id "
        "HAVING mention_count > ? AND fact_count <= ? "
        "ORDER BY mention_count DESC LIMIT 10",
        (min_mentions, max_facts),
    ).fetchall()
    return [
        {"entity_id": r[0], "canonical_name": r[1], "kind": r[2],
         "mention_count": r[3], "fact_count": r[4]}
        for r in rows
    ]


def auto_populate_questions(con: sqlite3.Connection, gaps: list[dict], ttl_days: int = 30) -> int:
    now = datetime.now(timezone.utc)
    ttl = (now + timedelta(days=ttl_days)).isoformat()
    now_iso = now.isoformat()
    added = 0
    for gap in gaps:
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
# Task 6: Salience Decay
# ---------------------------------------------------------------------------

DECAY_RATES = {
    "person": 0.001,
    "project": 0.005,
    "place": 0.003,
    "concept": 0.01,
    "default": 0.005,
}


def decay_salience(con: sqlite3.Connection) -> int:
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
        new_salience = max(0.05, new_salience)
        if abs(new_salience - salience) > 0.01:
            con.execute("UPDATE entities SET salience_score = ? WHERE id = ?", (round(new_salience, 4), eid))
            updated += 1
    return updated


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
# Main consolidation runner
# ---------------------------------------------------------------------------

def run_consolidation(db_path: str) -> dict:
    con = _open_connection(db_path)

    try:
        # Task 6: salience decay always runs (time-dependent, not data-dependent)
        con.execute("BEGIN IMMEDIATE")
        try:
            decay_count = decay_salience(con)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise

        # Task 3: skip-guard
        skip = _should_skip(con)
        if skip:
            skip["salience_decayed"] = decay_count
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

        # Mutating operations in a single transaction
        con.execute("BEGIN IMMEDIATE")
        try:
            closed = close_stale_questions(con)
            merges = process_approved_merges(con)
            questions_added = auto_populate_questions(con, gaps)
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
        "salience_decayed": decay_count,
        "valence_trend": trend,
        "extraction_efficiency": efficiency,
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
    print(f"Salience decayed: {report.get('salience_decayed', 0)}")
    print(f"Stale questions closed: {report['stale_questions_closed']}")
    print(f"Merges executed: {report['merges_executed']}")
    vt = report.get("valence_trend", {})
    print(f"Valence trend: {vt.get('trend', 'N/A')} (recent={vt.get('recent_7d_avg', 'N/A')}, overall={vt.get('overall_avg', 'N/A')}, points={vt.get('data_points', 0)})")
    ee = report.get("extraction_efficiency", {})
    print(f"Extraction efficiency: {ee.get('entities_per_1k_tokens', 0)} entities/1K tokens ({ee.get('total_entities', 0)} entities, {ee.get('total_tokens', 0)} tokens)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
