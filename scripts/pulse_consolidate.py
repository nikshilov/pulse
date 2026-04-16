#!/usr/bin/env python3
"""Pulse graph consolidation: dedup entities, close stale questions, execute merges, report stats."""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
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
                merged_aliases.update(json.loads(from_row[1]))
            if to_row and to_row[0]:
                merged_aliases.update(json.loads(to_row[0]))
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


def run_consolidation(db_path: str) -> dict:
    con = _open_connection(db_path)
    stats = entity_stats(con)
    duplicates = find_duplicate_candidates(con)
    closed = close_stale_questions(con)
    merges = process_approved_merges(con)
    con.close()

    return {
        "stats": stats,
        "duplicate_candidates": len(duplicates),
        "duplicates": duplicates[:20],
        "stale_questions_closed": closed,
        "merges_executed": merges,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Pulse graph consolidation")
    parser.add_argument("--db", required=True, help="Path to pulse database")
    args = parser.parse_args()

    report = run_consolidation(args.db)

    print("=== Consolidation Report ===")
    print(f"Entities: {report['stats']['total_entities']} ({report['stats']['entities_by_kind']})")
    print(f"Orphans: {report['stats']['orphan_entities']}")
    print(f"Relations: {report['stats']['total_relations']}")
    print(f"Facts: {report['stats']['total_facts']}")
    print(f"Duplicate candidates: {report['duplicate_candidates']}")
    for dup in report["duplicates"]:
        print(f"  {dup['entity_a_name']} <-> {dup['entity_b_name']} ({dup['similarity']})")
    print(f"Stale questions closed: {report['stale_questions_closed']}")
    print(f"Merges executed: {report['merges_executed']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
