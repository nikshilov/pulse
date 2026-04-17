#!/usr/bin/env python3
"""pulse_rewind — undo the graph mutations caused by a single observation.

Reads `graph_snapshots` in reverse chronological order for the given
observation_id and emits reverse operations inside a single BEGIN IMMEDIATE
transaction. Designed for the red-team scenario: a poisoned observation
slipped through triage + extract, mutated the graph, and now needs to be
scrubbed without touching anything else.

Usage:
  python scripts/pulse_rewind.py --db <path> --observation <obs_id> [--dry-run] [--yes]

Safety rails:
- FK violation during rewind → the whole tx rolls back; you get a precise
  error describing which row still references the target. No partial state.
- Snapshot rows for the observation are deleted at the end of the rewind so
  we don't leave orphan metadata.
- The rewind itself is recorded in erasure_log (op_kind='hard') for audit.
- The observation row is soft-erased (content nulled, redacted=1) so it
  won't be re-extracted, matching the shape of the existing erasure pattern.
"""

import argparse
import json
import sqlite3
import sys
import time
from typing import Optional


# FK-safe rewind order. Snapshot rows are emitted newest-first (id DESC), but
# we additionally group by op category to handle mixed-order logs: evidence
# and event_entities always come out before the rows they reference.
# This list orders reverse-op *priority* — lower priority runs first.
_REVERSE_OP_ORDER = [
    "insert_evidence",            # always safe to delete first
    "insert_event_entity",        # junction — before events or entities
    "insert_open_question",       # references entities
    "insert_entity_merge_proposal",  # references entities
    "insert_fact",                # references entities
    "insert_relation",            # references entities
    "update_relation",            # restore-in-place, no FK issue
    "insert_event",               # referenced by event_entities (already cleared)
    "update_entity",              # restore-in-place, no FK issue
    "insert_entity",              # referenced by facts/relations/events (already cleared)
]


def _table_columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]


def _reverse_sql(snapshot: sqlite3.Row) -> tuple[str, tuple]:
    """Translate one snapshot row into a (sql, params) reverse operation.

    Restore-in-place ops (update_entity, update_relation) build an UPDATE
    from the column set in before_json so we only touch columns that existed
    at the time of the mutation (schema-migration-safe).
    """
    op = snapshot["op"]
    row_id = snapshot["row_id"]
    if op == "insert_entity":
        return ("DELETE FROM entities WHERE id=?", (row_id,))
    if op == "insert_relation":
        return ("DELETE FROM relations WHERE id=?", (row_id,))
    if op == "insert_event":
        return ("DELETE FROM events WHERE id=?", (row_id,))
    if op == "insert_fact":
        return ("DELETE FROM facts WHERE id=?", (row_id,))
    if op == "insert_evidence":
        return ("DELETE FROM evidence WHERE id=?", (row_id,))
    if op == "insert_open_question":
        return ("DELETE FROM open_questions WHERE id=?", (row_id,))
    if op == "insert_entity_merge_proposal":
        return ("DELETE FROM entity_merge_proposals WHERE id=?", (row_id,))
    if op == "insert_event_entity":
        after = json.loads(snapshot["after_json"])
        return (
            "DELETE FROM event_entities WHERE event_id=? AND entity_id=?",
            (after["event_id"], after["entity_id"]),
        )
    if op in ("update_entity", "update_relation"):
        before = json.loads(snapshot["before_json"])
        # Rebuild a parameterized UPDATE that restores every non-id field.
        set_cols = [k for k in before.keys() if k != "id"]
        set_clause = ", ".join(f"{c}=?" for c in set_cols)
        params = tuple(before[c] for c in set_cols) + (row_id,)
        sql = f"UPDATE {snapshot['table_name']} SET {set_clause} WHERE id=?"
        return (sql, params)
    raise ValueError(f"unknown snapshot op: {op}")


def _sort_snapshots_for_rewind(snapshots: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """Order snapshots for FK-safe rewind.

    Primary: op category priority (see _REVERSE_OP_ORDER) so junction/leaf
    tables drop before parents.
    Secondary: snapshot id DESC inside each category so the newest mutation
    of a given kind rewinds first (matches intuitive "undo newest").
    """
    order_map = {op: i for i, op in enumerate(_REVERSE_OP_ORDER)}
    return sorted(
        snapshots,
        key=lambda s: (order_map.get(s["op"], 999), -s["id"]),
    )


def _soft_erase_observation(con: sqlite3.Connection, obs_id: int) -> bool:
    """Soft-erase the observation row: blank content, mark redacted=1.

    Returns True if the observation row existed. The observation row itself
    is not deleted — historical references (FK from extraction_jobs,
    observation_revisions) stay intact. This mirrors the existing redaction
    model in 003_observations.sql.
    """
    cur = con.execute(
        "UPDATE observations SET content_text=NULL, media_refs=NULL, "
        "metadata='{}', raw_json=NULL, redacted=1 WHERE id=?",
        (obs_id,),
    )
    return cur.rowcount > 0


def _log_erasure(con: sqlite3.Connection, obs_id: int, n_reverses: int) -> None:
    """Audit the rewind into erasure_log.

    op_kind='hard' since we're actually deleting graph rows, not just
    redacting. subject_kind='observation', subject_id=str(obs_id).
    """
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    con.execute(
        "INSERT INTO erasure_log (op_kind, subject_kind, subject_id, initiated_by, initiated_at, completed_at, note) "
        "VALUES (?,?,?,?,?,?,?)",
        ("hard", "observation", str(obs_id), "pulse_rewind_cli", now, now,
         f"rewound {n_reverses} graph mutations"),
    )


def rewind(
    db_path: str, obs_id: int, dry_run: bool = False, assume_yes: bool = False,
    out=sys.stdout,
) -> int:
    """Run rewind for obs_id. Returns 0 on success, non-zero on abort."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.isolation_level = None
    try:
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=5000")

        snapshots = list(con.execute(
            "SELECT * FROM graph_snapshots WHERE observation_id=? ORDER BY id DESC",
            (obs_id,),
        ).fetchall())

        if not snapshots:
            print(f"no graph_snapshots for observation {obs_id} — nothing to rewind",
                  file=out)
            return 0

        ordered = _sort_snapshots_for_rewind(snapshots)
        reverses = [_reverse_sql(s) for s in ordered]

        if dry_run:
            print(f"-- DRY RUN: {len(reverses)} reverse ops for observation {obs_id}",
                  file=out)
            for sql, params in reverses:
                print(f"{sql}  -- params={params}", file=out)
            print("-- (dry run — no changes made)", file=out)
            return 0

        if not assume_yes:
            prompt = f"Rewind {len(reverses)} mutations from observation {obs_id}? [y/N] "
            try:
                resp = input(prompt)
            except EOFError:
                resp = ""
            if resp.strip().lower() not in ("y", "yes"):
                print("aborted by user", file=out)
                return 1

        con.execute("BEGIN IMMEDIATE")
        try:
            for sql, params in reverses:
                try:
                    con.execute(sql, params)
                except sqlite3.IntegrityError as e:
                    # FK violation — another observation still references this row.
                    con.execute("ROLLBACK")
                    print(
                        f"ABORT: FK violation reversing op (sql={sql!r} params={params}): {e}. "
                        f"Another observation still references this row — rewind is NOT safe. "
                        f"No partial state written.",
                        file=out,
                    )
                    return 2
            # Clean up snapshot rows so we don't leave orphan metadata.
            con.execute("DELETE FROM graph_snapshots WHERE observation_id=?", (obs_id,))
            # Soft-erase the observation itself.
            _soft_erase_observation(con, obs_id)
            # Audit.
            _log_erasure(con, obs_id, len(reverses))
            con.execute("COMMIT")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise

        print(f"rewind complete: {len(reverses)} mutations reversed, "
              f"observation {obs_id} soft-erased, snapshot log cleared",
              file=out)
        return 0
    finally:
        con.close()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", required=True, help="path to pulse.db")
    p.add_argument("--observation", type=int, required=True,
                   help="observation_id whose graph effects to undo")
    p.add_argument("--dry-run", action="store_true",
                   help="print reverse SQL without executing")
    p.add_argument("--yes", action="store_true",
                   help="skip confirmation prompt")
    args = p.parse_args()
    return rewind(args.db, args.observation, dry_run=args.dry_run, assume_yes=args.yes)


if __name__ == "__main__":
    sys.exit(main())
