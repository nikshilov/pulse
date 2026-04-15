"""Phase 0 unblock — tests for per-observation tx isolation and PRAGMA fixes."""

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pulse_extract
from extract import prompts

MIGRATIONS = Path(__file__).resolve().parents[2] / "internal" / "store" / "migrations"


def _apply_migrations(db_path: Path) -> None:
    con = sqlite3.connect(db_path)
    for mig in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(mig.read_text())
    con.commit()
    con.close()


def test_open_connection_sets_pragmas(tmp_path):
    db = tmp_path / "p0.db"
    _apply_migrations(db)

    con = pulse_extract._open_connection(str(db))
    try:
        fk = con.execute("PRAGMA foreign_keys").fetchone()[0]
        bt = con.execute("PRAGMA busy_timeout").fetchone()[0]
        isolation = con.isolation_level
    finally:
        con.close()

    assert fk == 1, "foreign_keys must be ON"
    assert bt == 5000, "busy_timeout must be 5000 ms"
    assert isolation is None, (
        "isolation_level must be None so BEGIN/COMMIT are under our control"
    )
