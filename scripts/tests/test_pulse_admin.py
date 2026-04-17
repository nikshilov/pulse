"""Tests for pulse_admin.py — the safety-flag setter CLI.

Covers: mark-self (incl. singleton guard), protect/unprotect roundtrip,
show field coverage, list --sensitive markers, ambiguous-name behavior,
not-found behavior. All tests drive `main()` directly with a constructed
argv and capture stdout/stderr with capsys.
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import pulse_admin

MIGRATIONS = Path(__file__).resolve().parents[2] / "internal" / "store" / "migrations"


def _apply_migrations(db_path: Path) -> None:
    con = sqlite3.connect(db_path)
    for mig in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(mig.read_text())
    con.commit()
    con.close()


def _seed_entity(
    db_path: Path,
    canonical_name: str,
    kind: str = "person",
    aliases: list | None = None,
    salience: float = 0.5,
    emotional: float = 0.3,
    is_self: int = 0,
    do_not_probe: int = 0,
) -> int:
    con = sqlite3.connect(db_path)
    cur = con.execute(
        """INSERT INTO entities
           (canonical_name, kind, aliases, first_seen, last_seen,
            salience_score, emotional_weight, is_self, do_not_probe)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            canonical_name,
            kind,
            json.dumps(aliases or [], ensure_ascii=False),
            "2026-04-01T00:00:00Z",
            "2026-04-15T00:00:00Z",
            salience,
            emotional,
            is_self,
            do_not_probe,
        ),
    )
    entity_id = cur.lastrowid
    con.commit()
    con.close()
    return entity_id


def _get_flag(db_path: Path, entity_id: int, column: str) -> int:
    con = sqlite3.connect(db_path)
    row = con.execute(
        f"SELECT {column} FROM entities WHERE id = ?", (entity_id,)
    ).fetchone()
    con.close()
    return row[0]


# -------------------------- tests --------------------------


def test_mark_self_by_name(tmp_path, capsys):
    db = tmp_path / "p.db"
    _apply_migrations(db)
    eid = _seed_entity(db, "Nikita", emotional=0.9)

    rc = pulse_admin.main(
        ["--db", str(db), "--yes", "entity", "mark-self", "Nikita"]
    )
    out = capsys.readouterr().out

    assert rc == 0
    assert _get_flag(db, eid, "is_self") == 1
    assert "marked entity id=" in out
    assert "Nikita" in out
    assert "SQL: UPDATE entities SET is_self = 1" in out


def test_mark_self_rejects_second_self(tmp_path, capsys):
    db = tmp_path / "p.db"
    _apply_migrations(db)
    a = _seed_entity(db, "Nikita")
    b = _seed_entity(db, "Anna")

    rc1 = pulse_admin.main(
        ["--db", str(db), "--yes", "entity", "mark-self", "Nikita"]
    )
    assert rc1 == 0
    assert _get_flag(db, a, "is_self") == 1

    capsys.readouterr()  # clear buffer

    rc2 = pulse_admin.main(
        ["--db", str(db), "--yes", "entity", "mark-self", "Anna"]
    )
    err = capsys.readouterr().err

    assert rc2 == 3, f"expected exit=3 (SelfEntityAlreadySet), got {rc2}"
    assert _get_flag(db, b, "is_self") == 0, "second entity must remain unset"
    assert "self-entity already set" in err
    assert "Nikita" in err


def test_protect_unprotect_roundtrip(tmp_path, capsys):
    db = tmp_path / "p.db"
    _apply_migrations(db)
    eid = _seed_entity(db, "Kristina", emotional=0.87)

    assert _get_flag(db, eid, "do_not_probe") == 0

    rc = pulse_admin.main(["--db", str(db), "entity", "protect", "Kristina"])
    assert rc == 0
    assert _get_flag(db, eid, "do_not_probe") == 1

    # Idempotent re-protect: rc=0, remains 1.
    rc = pulse_admin.main(["--db", str(db), "entity", "protect", "Kristina"])
    assert rc == 0
    assert _get_flag(db, eid, "do_not_probe") == 1
    out = capsys.readouterr().out
    assert "already protected" in out

    rc = pulse_admin.main(["--db", str(db), "entity", "unprotect", "Kristina"])
    assert rc == 0
    assert _get_flag(db, eid, "do_not_probe") == 0

    # Idempotent re-unprotect.
    rc = pulse_admin.main(["--db", str(db), "entity", "unprotect", "Kristina"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "not currently protected" in out


def test_show_prints_all_fields(tmp_path, capsys):
    db = tmp_path / "p.db"
    _apply_migrations(db)
    _seed_entity(
        db,
        "Anna",
        aliases=["Аня", "Анна"],
        salience=0.92,
        emotional=0.87,
    )

    rc = pulse_admin.main(["--db", str(db), "entity", "show", "Anna"])
    out = capsys.readouterr().out

    assert rc == 0
    for field in (
        "id:",
        "canonical_name: Anna",
        "kind: person",
        "aliases:",
        "salience_score: 0.92",
        "emotional_weight: 0.87",
        "is_self: 0",
        "do_not_probe: 0",
        "last_seen:",
        "first_seen:",
    ):
        assert field in out, f"missing field in show output: {field!r}"
    assert "Аня" in out
    assert "Анна" in out


def test_list_sensitive_highlights_unprotected(tmp_path, capsys):
    db = tmp_path / "p.db"
    _apply_migrations(db)
    _seed_entity(db, "Kristina", emotional=0.87, do_not_probe=0)
    _seed_entity(db, "Anna", emotional=0.82, do_not_probe=1)
    # low-emo entity must NOT appear.
    _seed_entity(db, "Neighbor", emotional=0.2, do_not_probe=0)

    rc = pulse_admin.main(["--db", str(db), "entity", "list", "--sensitive"])
    out = capsys.readouterr().out

    assert rc == 0
    lines = out.splitlines()
    kristina_line = next(l for l in lines if "Kristina" in l)
    anna_line = next(l for l in lines if "Anna" in l)
    assert "[UNPROTECTED]" in kristina_line
    assert "[PROTECTED]" in anna_line
    # low-emo entity filtered out.
    assert "Neighbor" not in out


def test_ambiguous_name_exits_with_code_2(tmp_path, capsys):
    db = tmp_path / "p.db"
    _apply_migrations(db)
    _seed_entity(db, "Anna", kind="person")
    _seed_entity(db, "Anna", kind="person")  # duplicate dedup candidate

    rc = pulse_admin.main(["--db", str(db), "entity", "show", "Anna"])
    err = capsys.readouterr().err

    assert rc == 2
    assert "ambiguous" in err
    assert "--entity-id" in err


def test_entity_not_found_exits_with_code_1(tmp_path, capsys):
    db = tmp_path / "p.db"
    _apply_migrations(db)

    rc = pulse_admin.main(["--db", str(db), "entity", "show", "Nobody"])
    err = capsys.readouterr().err

    assert rc == 1
    assert "not found" in err


# -------------------------- bonus coverage --------------------------


def test_alias_resolution_finds_entity(tmp_path, capsys):
    db = tmp_path / "p.db"
    _apply_migrations(db)
    eid = _seed_entity(db, "Anna", aliases=["Аня", "Анна"])

    rc = pulse_admin.main(["--db", str(db), "entity", "show", "аня"])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"id: {eid}" in out


def test_entity_id_flag_bypasses_ambiguity(tmp_path, capsys):
    db = tmp_path / "p.db"
    _apply_migrations(db)
    a = _seed_entity(db, "Anna", kind="person")
    _seed_entity(db, "Anna", kind="person")

    rc = pulse_admin.main(
        ["--db", str(db), "--entity-id", str(a), "entity", "show", "Anna"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert f"id: {a}" in out


def test_list_self_empty_prints_none(tmp_path, capsys):
    db = tmp_path / "p.db"
    _apply_migrations(db)
    rc = pulse_admin.main(["--db", str(db), "entity", "list", "--self"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "(none)" in out


def test_list_protected_returns_marked_entities(tmp_path, capsys):
    db = tmp_path / "p.db"
    _apply_migrations(db)
    _seed_entity(db, "Kristina", do_not_probe=1, emotional=0.87)
    _seed_entity(db, "Plumber", do_not_probe=0, emotional=0.1)

    rc = pulse_admin.main(["--db", str(db), "entity", "list", "--protected"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Kristina" in out
    assert "Plumber" not in out


def test_mark_self_entity_not_found(tmp_path, capsys):
    db = tmp_path / "p.db"
    _apply_migrations(db)
    rc = pulse_admin.main(
        ["--db", str(db), "--yes", "entity", "mark-self", "Ghost"]
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert "not found" in err


def test_unmark_self_roundtrip(tmp_path, capsys):
    db = tmp_path / "p.db"
    _apply_migrations(db)
    eid = _seed_entity(db, "Nikita", is_self=1)

    rc = pulse_admin.main(["--db", str(db), "entity", "unmark-self", "Nikita"])
    assert rc == 0
    assert _get_flag(db, eid, "is_self") == 0

    # Re-running on already-unmarked is idempotent.
    rc = pulse_admin.main(["--db", str(db), "entity", "unmark-self", "Nikita"])
    assert rc == 0
