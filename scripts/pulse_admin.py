#!/usr/bin/env python3
"""Pulse admin CLI — setter path for the two safety flags on entities.

Context
-------
Two safety columns already exist in the schema but have no UX:

- entities.do_not_probe INTEGER NOT NULL DEFAULT 0  (migration 009)
  When 1: entity is filtered from seed match, BFS expansion, and the
  consolidator's auto_populate_questions step. An explicit user opt-out.

- entities.is_self INTEGER NOT NULL DEFAULT 0  (migration 010)
  When 1: entity doesn't receive the 1.5× anchor boost in retrieval._rank
  (prevents the owner self-entity from dominating its own retrieval).

Both default to 0. Without a setter path the gates are symbolic — they
defend air. This CLI lets the owner mark and inspect entities safely,
with dry-run SQL echo and explicit BEGIN IMMEDIATE / COMMIT.

Usage
-----
    python pulse_admin.py --db <path> entity mark-self <name>
    python pulse_admin.py --db <path> entity unmark-self <name>
    python pulse_admin.py --db <path> entity protect <name>
    python pulse_admin.py --db <path> entity unprotect <name>
    python pulse_admin.py --db <path> entity show <name>
    python pulse_admin.py --db <path> entity list --self
    python pulse_admin.py --db <path> entity list --protected
    python pulse_admin.py --db <path> entity list --sensitive

Global flags
------------
    --db PATH            required
    --entity-id N        resolve by primary key instead of name
    --yes / -y           skip confirmation prompts
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Optional


# ------------------------- connection -------------------------

def _open_connection(db_path: str) -> sqlite3.Connection:
    """Open sqlite3 connection with Pulse conventions.

    Mirrors pulse_extract._open_connection / pulse_consolidate._open_connection:
    - PRAGMA busy_timeout=5000: survive WAL contention with the Go ingest process.
    - PRAGMA foreign_keys=ON: schema assumes FK enforcement.
    - isolation_level=None: manual BEGIN IMMEDIATE / COMMIT so mutations are
      scoped and visible atomically.
    """
    con = sqlite3.connect(db_path)
    con.isolation_level = None
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=5000")
    return con


# ------------------------- exceptions -------------------------

class AdminError(Exception):
    """Base class. Subclasses carry an intended process exit code."""
    exit_code: int = 1


class EntityNotFoundError(AdminError):
    exit_code = 1


class AmbiguousEntityError(AdminError):
    exit_code = 2

    def __init__(self, matches: list[sqlite3.Row]):
        self.matches = matches
        super().__init__(f"ambiguous: {len(matches)} matches")


class SelfEntityAlreadySetError(AdminError):
    exit_code = 3


# ------------------------- resolution -------------------------

def _row_to_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


def _resolve_entity(
    con: sqlite3.Connection,
    name: Optional[str],
    entity_id: Optional[int],
) -> dict:
    """Resolve a single entity by primary key or by name (canonical / alias).

    - If entity_id is given, resolution is unambiguous. Raises
      EntityNotFoundError if the id does not exist.
    - Otherwise: try exact canonical_name match first (case-insensitive),
      then alias match over the JSON aliases array (case-insensitive).
    - If 0 candidates total: raises EntityNotFoundError.
    - If >1 candidates: raises AmbiguousEntityError with the full match list.
    """
    if entity_id is not None:
        row = con.execute(
            "SELECT * FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if row is None:
            raise EntityNotFoundError(f"entity id={entity_id} not found")
        return _row_to_dict(row)

    if not name:
        raise EntityNotFoundError("no name or --entity-id provided")

    # Exact canonical match (case-insensitive) first.
    canonical_rows = con.execute(
        "SELECT * FROM entities WHERE LOWER(canonical_name) = LOWER(?)",
        (name,),
    ).fetchall()

    matches: list[sqlite3.Row] = list(canonical_rows)

    # If no canonical hits, search aliases JSON (case-insensitive).
    if not matches:
        needle = name.lower()
        for row in con.execute("SELECT * FROM entities WHERE aliases IS NOT NULL"):
            raw = row["aliases"]
            try:
                alias_list = json.loads(raw) if raw else []
            except (TypeError, json.JSONDecodeError):
                alias_list = []
            if not isinstance(alias_list, list):
                continue
            if any(isinstance(a, str) and a.lower() == needle for a in alias_list):
                matches.append(row)

    if not matches:
        raise EntityNotFoundError(f"entity '{name}' not found")
    if len(matches) > 1:
        raise AmbiguousEntityError(matches)
    return _row_to_dict(matches[0])


# ------------------------- helpers -------------------------

def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        # Non-interactive without --yes → refuse for safety.
        print(f"refusing non-interactive confirmation (pass --yes): {prompt}")
        return False
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def _echo_sql(sql: str, params: tuple) -> None:
    """Echo the SQL that will run. params are quoted only for human display."""
    shown = sql
    for p in params:
        shown = shown.replace("?", repr(p), 1)
    print(f"SQL: {shown}")


def _execute_mutation(con: sqlite3.Connection, sql: str, params: tuple) -> None:
    _echo_sql(sql, params)
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute(sql, params)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def _fmt_aliases(raw: Optional[str]) -> str:
    if not raw:
        return "[]"
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return raw
    return json.dumps(parsed, ensure_ascii=False)


# ------------------------- commands -------------------------

def cmd_mark_self(con: sqlite3.Connection, args) -> int:
    try:
        ent = _resolve_entity(con, args.name, args.entity_id)
    except AmbiguousEntityError as e:
        _print_ambiguous(e.matches)
        return e.exit_code
    except EntityNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.exit_code

    # Enforce singleton: only one entity may carry is_self=1 at a time.
    row = con.execute(
        "SELECT id, canonical_name FROM entities WHERE is_self = 1 AND id != ?",
        (ent["id"],),
    ).fetchone()
    if row is not None:
        print(
            f"warning: self-entity already set to "
            f"'{row['canonical_name']}' (id={row['id']}). "
            f"Unmark it first with `entity unmark-self`.",
            file=sys.stderr,
        )
        return SelfEntityAlreadySetError.exit_code

    if ent.get("is_self") == 1:
        print(f"entity id={ent['id']} canonical_name='{ent['canonical_name']}' already marked as self")
        return 0

    if not _confirm(
        f"mark entity id={ent['id']} '{ent['canonical_name']}' as self?",
        args.yes,
    ):
        print("aborted")
        return 0

    _execute_mutation(
        con,
        "UPDATE entities SET is_self = 1 WHERE id = ?",
        (ent["id"],),
    )
    print(f"marked entity id={ent['id']} canonical_name='{ent['canonical_name']}' as self")
    return 0


def cmd_unmark_self(con: sqlite3.Connection, args) -> int:
    try:
        ent = _resolve_entity(con, args.name, args.entity_id)
    except AmbiguousEntityError as e:
        _print_ambiguous(e.matches)
        return e.exit_code
    except EntityNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.exit_code

    if ent.get("is_self") != 1:
        print(f"entity id={ent['id']} canonical_name='{ent['canonical_name']}' not currently self")
        return 0

    _execute_mutation(
        con,
        "UPDATE entities SET is_self = 0 WHERE id = ?",
        (ent["id"],),
    )
    print(f"unmarked entity id={ent['id']} canonical_name='{ent['canonical_name']}' as self")
    return 0


def cmd_protect(con: sqlite3.Connection, args) -> int:
    try:
        ent = _resolve_entity(con, args.name, args.entity_id)
    except AmbiguousEntityError as e:
        _print_ambiguous(e.matches)
        return e.exit_code
    except EntityNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.exit_code

    if ent.get("do_not_probe") == 1:
        print(f"entity id={ent['id']} canonical_name='{ent['canonical_name']}' already protected")
        return 0

    _execute_mutation(
        con,
        "UPDATE entities SET do_not_probe = 1 WHERE id = ?",
        (ent["id"],),
    )
    print(f"protected entity id={ent['id']} canonical_name='{ent['canonical_name']}' (do_not_probe=1)")
    return 0


def cmd_unprotect(con: sqlite3.Connection, args) -> int:
    try:
        ent = _resolve_entity(con, args.name, args.entity_id)
    except AmbiguousEntityError as e:
        _print_ambiguous(e.matches)
        return e.exit_code
    except EntityNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.exit_code

    if ent.get("do_not_probe") != 1:
        print(f"entity id={ent['id']} canonical_name='{ent['canonical_name']}' not currently protected")
        return 0

    _execute_mutation(
        con,
        "UPDATE entities SET do_not_probe = 0 WHERE id = ?",
        (ent["id"],),
    )
    print(f"unprotected entity id={ent['id']} canonical_name='{ent['canonical_name']}' (do_not_probe=0)")
    return 0


def cmd_show(con: sqlite3.Connection, args) -> int:
    try:
        ent = _resolve_entity(con, args.name, args.entity_id)
    except AmbiguousEntityError as e:
        _print_ambiguous(e.matches)
        return e.exit_code
    except EntityNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.exit_code

    print(f"id: {ent['id']}")
    print(f"canonical_name: {ent['canonical_name']}")
    print(f"kind: {ent['kind']}")
    print(f"aliases: {_fmt_aliases(ent.get('aliases'))}")
    print(f"salience_score: {ent.get('salience_score')}")
    print(f"emotional_weight: {ent.get('emotional_weight')}")
    print(f"is_self: {ent.get('is_self', 0)}")
    print(f"do_not_probe: {ent.get('do_not_probe', 0)}")
    print(f"last_seen: {ent.get('last_seen')}")
    print(f"first_seen: {ent.get('first_seen')}")
    return 0


def cmd_list(con: sqlite3.Connection, args) -> int:
    flags = [args.self, args.protected, args.sensitive]
    if sum(bool(f) for f in flags) != 1:
        print(
            "error: specify exactly one of --self / --protected / --sensitive",
            file=sys.stderr,
        )
        return 2

    if args.self:
        rows = con.execute(
            "SELECT id, canonical_name, kind, emotional_weight "
            "FROM entities WHERE is_self = 1 ORDER BY id"
        ).fetchall()
        _print_list(rows, header=("id", "canonical_name", "kind", "emotional_weight"))
        return 0

    if args.protected:
        rows = con.execute(
            "SELECT id, canonical_name, kind, emotional_weight "
            "FROM entities WHERE do_not_probe = 1 ORDER BY id"
        ).fetchall()
        _print_list(rows, header=("id", "canonical_name", "kind", "emotional_weight"))
        return 0

    # --sensitive: emotional_weight > 0.6, flag protected status so the owner
    # can see e.g. "Kristina emo=0.87 [UNPROTECTED]" and act.
    rows = con.execute(
        "SELECT id, canonical_name, kind, emotional_weight, do_not_probe "
        "FROM entities WHERE emotional_weight > 0.6 "
        "ORDER BY emotional_weight DESC, id"
    ).fetchall()
    if not rows:
        print("(none)")
        return 0
    print(f"{'id':<6} {'canonical_name':<30} {'kind':<10} {'emo':<6} status")
    for r in rows:
        status = "[PROTECTED]" if r["do_not_probe"] == 1 else "[UNPROTECTED]"
        emo = f"{r['emotional_weight']:.2f}"
        print(f"{r['id']:<6} {r['canonical_name']:<30} {r['kind']:<10} {emo:<6} {status}")
    return 0


# ------------------------- output helpers -------------------------

def _print_list(rows: list[sqlite3.Row], header: tuple) -> None:
    if not rows:
        print("(none)")
        return
    print(f"{header[0]:<6} {header[1]:<30} {header[2]:<10} {header[3]}")
    for r in rows:
        emo = f"{r['emotional_weight']:.2f}"
        print(f"{r['id']:<6} {r['canonical_name']:<30} {r['kind']:<10} {emo}")


def _print_ambiguous(matches: list[sqlite3.Row]) -> None:
    print(
        f"ambiguous: specify by ID using --entity-id <id>. "
        f"{len(matches)} matches:",
        file=sys.stderr,
    )
    print(f"{'id':<6} {'canonical_name':<30} {'kind':<10} salience", file=sys.stderr)
    for r in matches:
        sal = f"{r['salience_score']:.2f}" if r["salience_score"] is not None else "?"
        print(
            f"{r['id']:<6} {r['canonical_name']:<30} {r['kind']:<10} {sal}",
            file=sys.stderr,
        )


# ------------------------- argparse wiring -------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pulse_admin",
        description="Admin CLI for setting entity safety flags (is_self, do_not_probe).",
    )
    parser.add_argument("--db", required=True, help="Path to pulse.db")
    parser.add_argument("--entity-id", type=int, default=None,
                        help="Resolve entity by primary key instead of name.")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Skip confirmation prompts.")

    sub = parser.add_subparsers(dest="resource", required=True)
    entity_parser = sub.add_parser("entity", help="Entity operations")
    entity_sub = entity_parser.add_subparsers(dest="action", required=True)

    def _add_name_cmd(name: str, help_text: str):
        p = entity_sub.add_parser(name, help=help_text)
        p.add_argument("name", nargs="?", default=None,
                       help="canonical_name or alias (case-insensitive)")
        return p

    _add_name_cmd("mark-self", "Set is_self=1 on the resolved entity")
    _add_name_cmd("unmark-self", "Set is_self=0 on the resolved entity")
    _add_name_cmd("protect", "Set do_not_probe=1 on the resolved entity")
    _add_name_cmd("unprotect", "Set do_not_probe=0 on the resolved entity")
    _add_name_cmd("show", "Print all fields of the resolved entity")

    p_list = entity_sub.add_parser("list", help="List entities by flag")
    group = p_list.add_mutually_exclusive_group(required=True)
    group.add_argument("--self", action="store_true",
                       help="All entities with is_self=1")
    group.add_argument("--protected", action="store_true",
                       help="All entities with do_not_probe=1")
    group.add_argument("--sensitive", action="store_true",
                       help="All entities with emotional_weight > 0.6")

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        con = _open_connection(args.db)
    except sqlite3.Error as e:
        print(f"error opening db: {e}", file=sys.stderr)
        return 1

    try:
        if args.resource == "entity":
            if args.action == "mark-self":
                return cmd_mark_self(con, args)
            if args.action == "unmark-self":
                return cmd_unmark_self(con, args)
            if args.action == "protect":
                return cmd_protect(con, args)
            if args.action == "unprotect":
                return cmd_unprotect(con, args)
            if args.action == "show":
                return cmd_show(con, args)
            if args.action == "list":
                return cmd_list(con, args)
        parser.print_help(sys.stderr)
        return 2
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
