"""Tests for scripts/secure_db.py — SQLCipher at-rest encryption CLI.

These tests require `pysqlcipher3` to be installed. The whole file is
skipped cleanly if the dep is absent — `secure_db.py` itself gates the
import at runtime and exits 127 with a helpful message, so the CLI
stays usable as a helpful error message even without the dep.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

# Skip the whole module if neither SQLCipher binding is available.
# `secure_db.py` accepts either `pysqlcipher3` (the canonical name, which is
# often broken on modern macOS) or `sqlcipher3-wheels` (drop-in replacement
# under the `sqlcipher3` import name).
_has_pysqlcipher3 = False
_has_sqlcipher3 = False
try:
    import pysqlcipher3  # noqa: F401
    _has_pysqlcipher3 = True
except ImportError:
    pass
try:
    import sqlcipher3  # noqa: F401
    _has_sqlcipher3 = True
except ImportError:
    pass
if not (_has_pysqlcipher3 or _has_sqlcipher3):
    pytest.skip(
        "neither pysqlcipher3 nor sqlcipher3 installed — skipping secure_db tests",
        allow_module_level=True,
    )

CLI = Path(__file__).parent.parent / "secure_db.py"


def _run(args, env=None):
    """Run the CLI, capture output, return CompletedProcess."""
    e = os.environ.copy()
    if env:
        e.update(env)
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        capture_output=True,
        text=True,
        env=e,
    )


def _seed_plaintext_db(path: Path, rows: list[tuple[int, str]]) -> None:
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
    con.executemany("INSERT INTO t (id, name) VALUES (?, ?)", rows)
    con.commit()
    con.close()


def _read_plaintext_rows(path: Path) -> list[tuple[int, str]]:
    con = sqlite3.connect(str(path))
    try:
        cur = con.execute("SELECT id, name FROM t ORDER BY id")
        return list(cur.fetchall())
    finally:
        con.close()


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_help_lists_subcommands():
    result = _run(["--help"])
    assert result.returncode == 0, result.stderr
    for kw in ("encrypt", "decrypt", "verify", "rotate"):
        assert kw in result.stdout


def test_encrypt_creates_destination_file(tmp_path):
    plain = tmp_path / "plain.db"
    enc = tmp_path / "enc.db"
    _seed_plaintext_db(plain, [(1, "alice"), (2, "bob")])

    r = _run(
        ["encrypt", str(plain), str(enc), "--key-env", "TEST_KEY"],
        env={"TEST_KEY": "hunter2hunter2hunter2hunter2hunter2hunter2"},
    )
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert enc.exists()
    assert enc.stat().st_size > 0
    # Encrypted file must NOT be readable as plain SQLite
    with pytest.raises(sqlite3.DatabaseError):
        con = sqlite3.connect(str(enc))
        con.execute("SELECT count(*) FROM sqlite_master").fetchone()
        con.close()


def test_decrypt_roundtrip_preserves_rows(tmp_path):
    plain = tmp_path / "plain.db"
    enc = tmp_path / "enc.db"
    restored = tmp_path / "restored.db"
    rows = [(1, "alice"), (2, "bob"), (42, "gravity")]
    _seed_plaintext_db(plain, rows)

    env = {"TEST_KEY": "a-long-reasonable-passphrase-41-chars-min!"}

    r = _run(["encrypt", str(plain), str(enc), "--key-env", "TEST_KEY"], env=env)
    assert r.returncode == 0, r.stderr

    plain.unlink()

    r = _run(["decrypt", str(enc), str(restored), "--key-env", "TEST_KEY"], env=env)
    assert r.returncode == 0, r.stderr
    assert restored.exists()
    assert _read_plaintext_rows(restored) == rows


def test_wrong_key_on_decrypt_fails_cleanly(tmp_path):
    plain = tmp_path / "plain.db"
    enc = tmp_path / "enc.db"
    restored = tmp_path / "restored.db"
    _seed_plaintext_db(plain, [(1, "alice")])

    r = _run(
        ["encrypt", str(plain), str(enc), "--key-env", "K1"],
        env={"K1": "correct-key-correct-key-correct-key-xxxx"},
    )
    assert r.returncode == 0, r.stderr

    r = _run(
        ["decrypt", str(enc), str(restored), "--key-env", "K2"],
        env={"K2": "wrong-key-wrong-key-wrong-key-zzzzzzzzzz"},
    )
    assert r.returncode != 0
    # No half-written output file
    assert not restored.exists()


def test_rotate_key_preserves_data(tmp_path):
    plain = tmp_path / "plain.db"
    enc = tmp_path / "enc.db"
    restored = tmp_path / "restored.db"
    rows = [(1, "alice"), (2, "bob")]
    _seed_plaintext_db(plain, rows)

    env = {
        "K1": "original-key-xxxxxxxxxxxxxxxxxxxxxxxxxx",
        "K2": "rotated-key-yyyyyyyyyyyyyyyyyyyyyyyyyyyy",
    }

    r = _run(["encrypt", str(plain), str(enc), "--key-env", "K1"], env=env)
    assert r.returncode == 0, r.stderr

    r = _run(
        ["rotate", str(enc), "--old-key-env", "K1", "--new-key-env", "K2"],
        env=env,
    )
    assert r.returncode == 0, r.stderr

    # New key works
    r = _run(["verify", str(enc), "--key-env", "K2"], env=env)
    assert r.returncode == 0, r.stderr

    # Old key fails
    r = _run(["verify", str(enc), "--key-env", "K1"], env=env)
    assert r.returncode != 0

    # Data intact after rotation
    r = _run(["decrypt", str(enc), str(restored), "--key-env", "K2"], env=env)
    assert r.returncode == 0, r.stderr
    assert _read_plaintext_rows(restored) == rows


def test_verify_good_key_returns_zero(tmp_path):
    plain = tmp_path / "plain.db"
    enc = tmp_path / "enc.db"
    _seed_plaintext_db(plain, [(1, "alice")])

    env = {"K": "good-key-good-key-good-key-good-key-111111"}
    assert _run(["encrypt", str(plain), str(enc), "--key-env", "K"], env=env).returncode == 0

    r = _run(["verify", str(enc), "--key-env", "K"], env=env)
    assert r.returncode == 0
    assert "OK" in r.stdout


def test_verify_wrong_key_returns_nonzero(tmp_path):
    plain = tmp_path / "plain.db"
    enc = tmp_path / "enc.db"
    _seed_plaintext_db(plain, [(1, "alice")])

    r = _run(
        ["encrypt", str(plain), str(enc), "--key-env", "GOOD"],
        env={"GOOD": "good-key-good-key-good-key-good-key-111111"},
    )
    assert r.returncode == 0, r.stderr

    r = _run(
        ["verify", str(enc), "--key-env", "BAD"],
        env={"BAD": "nope-nope-nope-nope-nope-nope-nope-nope"},
    )
    assert r.returncode != 0


def test_refuses_to_overwrite_without_force(tmp_path):
    plain = tmp_path / "plain.db"
    enc = tmp_path / "enc.db"
    _seed_plaintext_db(plain, [(1, "alice")])
    enc.write_bytes(b"existing content - must not clobber")
    original_bytes = enc.read_bytes()

    r = _run(
        ["encrypt", str(plain), str(enc), "--key-env", "K"],
        env={"K": "some-key-some-key-some-key-some-key-999999"},
    )
    assert r.returncode != 0
    assert enc.read_bytes() == original_bytes

    # With --force, overwrite succeeds
    r = _run(
        ["encrypt", str(plain), str(enc), "--key-env", "K", "--force"],
        env={"K": "some-key-some-key-some-key-some-key-999999"},
    )
    assert r.returncode == 0, r.stderr
    assert enc.read_bytes() != original_bytes


def test_key_file_source(tmp_path):
    plain = tmp_path / "plain.db"
    enc = tmp_path / "enc.db"
    keyfile = tmp_path / "key.txt"
    _seed_plaintext_db(plain, [(1, "alice")])
    keyfile.write_text("file-sourced-passphrase-xxxxxxxxxxxxxxx\n")

    r = _run(["encrypt", str(plain), str(enc), "--key-file", str(keyfile)])
    assert r.returncode == 0, r.stderr

    r = _run(["verify", str(enc), "--key-file", str(keyfile)])
    assert r.returncode == 0, r.stderr
