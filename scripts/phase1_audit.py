#!/usr/bin/env python3
"""Phase 1 pre-migration safety audit.

Verifies that the current graph has no duplicate (from, to, kind) relations
or (entity_id, text) facts. If any duplicate exists, migration 006 will fail.
Run against a COPY of pulse-dev.db, never production.
"""

import argparse
import sqlite3
import sys


def audit(db_path: str) -> int:
    con = sqlite3.connect(db_path)
    try:
        rel_dups = con.execute(
            """SELECT from_entity_id, to_entity_id, kind, COUNT(*) AS n
               FROM relations
               GROUP BY from_entity_id, to_entity_id, kind
               HAVING n > 1"""
        ).fetchall()
        fact_dups = con.execute(
            """SELECT entity_id, text, COUNT(*) AS n
               FROM facts
               GROUP BY entity_id, text
               HAVING n > 1"""
        ).fetchall()
    finally:
        con.close()

    print(f"relations duplicates: {len(rel_dups)}")
    for row in rel_dups[:10]:
        print(f"  (from={row[0]}, to={row[1]}, kind={row[2]!r}) x{row[3]}")
    if len(rel_dups) > 10:
        print(f"  ... {len(rel_dups) - 10} more")

    print(f"facts duplicates: {len(fact_dups)}")
    for row in fact_dups[:10]:
        text_preview = (row[1] or "")[:60]
        print(f"  (entity_id={row[0]}, text={text_preview!r}) x{row[2]}")
    if len(fact_dups) > 10:
        print(f"  ... {len(fact_dups) - 10} more")

    if rel_dups or fact_dups:
        print("FAIL: duplicates found — migration 006 would fail on deploy")
        return 1
    print("OK: no duplicates, safe to migrate")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", required=True, help="Path to SQLite DB (use a COPY, not pulse-dev live)")
    args = p.parse_args()
    return audit(args.db)


if __name__ == "__main__":
    sys.exit(main())
