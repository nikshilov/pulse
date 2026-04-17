#!/usr/bin/env python3
"""secure_db.py — SQLCipher at-rest encryption CLI for Pulse databases.

Encrypt, decrypt, verify, and rotate keys on Pulse SQLite databases using
SQLCipher. This tool is orthogonal to the rest of Pulse — it operates on
files, not at runtime. Workflow:

    # Step away:
    python scripts/secure_db.py encrypt pulse.db pulse.db.enc --key-env PULSE_DB_KEY
    rm pulse.db

    # Come back:
    python scripts/secure_db.py decrypt pulse.db.enc pulse.db --key-env PULSE_DB_KEY
    # run Pulse as usual — _open_connection still reads plaintext

Requires: pip install pysqlcipher3 (optional Pulse dep).
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

# Try both bindings — pysqlcipher3 is the documented name; sqlcipher3-wheels
# ships a drop-in replacement under `sqlcipher3` that builds cleanly on modern
# macOS (pysqlcipher3 often fails against recent OpenSSL). Either works.
try:
    from pysqlcipher3 import dbapi2 as sqlcipher  # type: ignore
except ImportError:
    try:
        from sqlcipher3 import dbapi2 as sqlcipher  # type: ignore
    except ImportError:  # pragma: no cover — runtime guard
        print(
            "ERROR: pysqlcipher3 not installed. Run: pip install pysqlcipher3 "
            "(or pip install sqlcipher3-wheels on macOS if pysqlcipher3 fails to build).",
            file=sys.stderr,
        )
        sys.exit(127)


# ---------------------------------------------------------------------------
# key sourcing
# ---------------------------------------------------------------------------

def _resolve_key(args, *, prompt: str = "Enter SQLCipher key: ") -> str:
    """Resolve the key from (priority): --key-env > --key-file > prompt."""
    env_var = getattr(args, "key_env", None)
    if env_var:
        key = os.environ.get(env_var)
        if key is None or key == "":
            print(
                f"ERROR: environment variable '{env_var}' is not set or empty",
                file=sys.stderr,
            )
            sys.exit(2)
        return key

    key_file = getattr(args, "key_file", None)
    if key_file:
        p = Path(key_file)
        if not p.exists():
            print(f"ERROR: key file not found: {key_file}", file=sys.stderr)
            sys.exit(2)
        # rstrip only trailing newline — preserve any intentional trailing spaces
        return p.read_text().rstrip("\n").rstrip("\r")

    return getpass.getpass(prompt)


def _resolve_two_keys(args) -> tuple[str, str]:
    """Resolve old key + new key for rotate."""
    # old
    if args.old_key_env:
        key = os.environ.get(args.old_key_env)
        if key is None or key == "":
            print(
                f"ERROR: environment variable '{args.old_key_env}' is not set or empty",
                file=sys.stderr,
            )
            sys.exit(2)
        old_key = key
    elif args.old_key_file:
        p = Path(args.old_key_file)
        if not p.exists():
            print(f"ERROR: old key file not found: {args.old_key_file}", file=sys.stderr)
            sys.exit(2)
        old_key = p.read_text().rstrip("\n").rstrip("\r")
    else:
        old_key = getpass.getpass("Enter OLD SQLCipher key: ")

    if args.new_key_env:
        key = os.environ.get(args.new_key_env)
        if key is None or key == "":
            print(
                f"ERROR: environment variable '{args.new_key_env}' is not set or empty",
                file=sys.stderr,
            )
            sys.exit(2)
        new_key = key
    elif args.new_key_file:
        p = Path(args.new_key_file)
        if not p.exists():
            print(f"ERROR: new key file not found: {args.new_key_file}", file=sys.stderr)
            sys.exit(2)
        new_key = p.read_text().rstrip("\n").rstrip("\r")
    else:
        new_key = getpass.getpass("Enter NEW SQLCipher key: ")

    return old_key, new_key


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _escape_pragma_key(key: str) -> str:
    """Escape single quotes for inline PRAGMA key usage.

    SQLCipher's PRAGMA key does not accept bound parameters, so we must
    embed the key in SQL. We single-quote and double-up internal quotes.
    """
    return key.replace("'", "''")


def _pragma_key_stmt(key: str) -> str:
    """Return a PRAGMA key statement. SQLCipher treats a quoted string as a
    passphrase (runs PBKDF2 internally). If the key starts with "x'" and ends
    with "'" it is treated as raw hex. We always use passphrase form unless the
    caller has explicitly formatted raw hex.
    """
    # Allow raw hex escape: if user passed x'HEX'
    if key.startswith("x'") and key.endswith("'"):
        return f"PRAGMA key = {key};"
    return f"PRAGMA key = '{_escape_pragma_key(key)}';"


def _pragma_rekey_stmt(key: str) -> str:
    if key.startswith("x'") and key.endswith("'"):
        return f"PRAGMA rekey = {key};"
    return f"PRAGMA rekey = '{_escape_pragma_key(key)}';"


def _chmod_600(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # best-effort on unusual filesystems


def _warn_backup() -> None:
    print(
        "WARNING: SQLCipher key is now the only thing protecting your memory. "
        "Back it up somewhere separate from the DB.",
        file=sys.stderr,
    )


def _refuse_overwrite(dest: Path, force: bool) -> None:
    if dest.exists() and not force:
        print(
            f"ERROR: destination exists: {dest} (pass --force to overwrite)",
            file=sys.stderr,
        )
        sys.exit(3)
    if dest.exists() and force:
        dest.unlink()


def _verify_encrypted(path: Path, key: str) -> bool:
    """Return True if path opens with key and reports a valid schema."""
    try:
        con = sqlcipher.connect(str(path))
        try:
            con.execute(_pragma_key_stmt(key))
            # Force decryption attempt
            cur = con.execute("SELECT count(*) FROM sqlite_master")
            cur.fetchone()
            return True
        finally:
            con.close()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_encrypt(args) -> int:
    src = Path(args.source)
    dst = Path(args.dest)

    if not src.exists():
        print(f"ERROR: source not found: {src}", file=sys.stderr)
        return 2

    _refuse_overwrite(dst, args.force)
    key = _resolve_key(args)
    if not key:
        print("ERROR: empty key", file=sys.stderr)
        return 2

    # Per SQLCipher docs (sqlcipher_export):
    #   1. open the PLAINTEXT db via sqlcipher (no PRAGMA key — it's plaintext)
    #   2. ATTACH the encrypted destination with a KEY
    #   3. SELECT sqlcipher_export('encrypted') copies schema+rows
    con = sqlcipher.connect(str(src))
    try:
        escaped = _escape_pragma_key(key)
        con.execute(f"ATTACH DATABASE ? AS encrypted KEY '{escaped}';", (str(dst),))
        con.execute("SELECT sqlcipher_export('encrypted');")
        con.execute("DETACH DATABASE encrypted;")
        con.commit()
    except Exception as exc:
        con.close()
        # Clean up half-written destination on failure
        if dst.exists():
            try:
                dst.unlink()
            except OSError:
                pass
        print(f"ERROR: encrypt failed: {exc}", file=sys.stderr)
        return 1
    con.close()

    _chmod_600(dst)

    # Verify round-trip before declaring success
    if not _verify_encrypted(dst, key):
        print("ERROR: post-encrypt verification failed — destination is unusable", file=sys.stderr)
        try:
            dst.unlink()
        except OSError:
            pass
        return 1

    print(f"OK: encrypted {src} -> {dst}")
    _warn_backup()
    return 0


def cmd_decrypt(args) -> int:
    src = Path(args.source)
    dst = Path(args.dest)

    if not src.exists():
        print(f"ERROR: source not found: {src}", file=sys.stderr)
        return 2

    _refuse_overwrite(dst, args.force)
    key = _resolve_key(args)
    if not key:
        print("ERROR: empty key", file=sys.stderr)
        return 2

    # 1. open encrypted with key
    # 2. ATTACH plaintext dest with empty KEY ''
    # 3. sqlcipher_export('plaintext')
    con = sqlcipher.connect(str(src))
    try:
        con.execute(_pragma_key_stmt(key))
        # Validate key BEFORE writing output — reading sqlite_master fails on wrong key
        con.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except Exception as exc:
        con.close()
        print(f"ERROR: cannot open encrypted source (wrong key?): {exc}", file=sys.stderr)
        return 1

    try:
        con.execute("ATTACH DATABASE ? AS plaintext KEY '';", (str(dst),))
        con.execute("SELECT sqlcipher_export('plaintext');")
        con.execute("DETACH DATABASE plaintext;")
        con.commit()
    except Exception as exc:
        con.close()
        if dst.exists():
            try:
                dst.unlink()
            except OSError:
                pass
        print(f"ERROR: decrypt failed: {exc}", file=sys.stderr)
        return 1
    con.close()

    _chmod_600(dst)
    print(f"OK: decrypted {src} -> {dst}")
    return 0


def cmd_verify(args) -> int:
    src = Path(args.source)
    if not src.exists():
        print(f"ERROR: source not found: {src}", file=sys.stderr)
        return 2
    key = _resolve_key(args)
    if _verify_encrypted(src, key):
        print(f"OK: {src} opens with provided key")
        return 0
    print(f"FAIL: {src} did not open with provided key (wrong key or not a SQLCipher db)", file=sys.stderr)
    return 1


def cmd_rotate(args) -> int:
    src = Path(args.source)
    if not src.exists():
        print(f"ERROR: source not found: {src}", file=sys.stderr)
        return 2

    old_key, new_key = _resolve_two_keys(args)
    if not new_key:
        print("ERROR: empty new key", file=sys.stderr)
        return 2

    con = sqlcipher.connect(str(src))
    try:
        con.execute(_pragma_key_stmt(old_key))
        # validate old key
        con.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except Exception as exc:
        con.close()
        print(f"ERROR: cannot open with old key: {exc}", file=sys.stderr)
        return 1

    try:
        con.execute(_pragma_rekey_stmt(new_key))
        con.commit()
    except Exception as exc:
        con.close()
        print(f"ERROR: rekey failed: {exc}", file=sys.stderr)
        return 1
    con.close()

    # Verify new key works
    if not _verify_encrypted(src, new_key):
        print("ERROR: post-rotate verification with NEW key failed", file=sys.stderr)
        return 1

    print(f"OK: rotated key on {src}")
    _warn_backup()
    return 0


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def _add_key_args(p: argparse.ArgumentParser) -> None:
    g = p.add_mutually_exclusive_group()
    g.add_argument("--key-env", dest="key_env", metavar="VAR", help="read key from environment variable VAR")
    g.add_argument("--key-file", dest="key_file", metavar="PATH", help="read key from file (trailing newline trimmed)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="secure_db.py",
        description="SQLCipher at-rest encryption for Pulse databases. "
        "Encrypts/decrypts/rotates SQLite files on disk. "
        "Orthogonal to the rest of Pulse — existing _open_connection still reads plaintext.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    enc = sub.add_parser("encrypt", help="encrypt a plaintext SQLite db into an encrypted one")
    enc.add_argument("source", help="plaintext .db to read")
    enc.add_argument("dest", help="encrypted .db to create")
    enc.add_argument("--force", action="store_true", help="overwrite destination if it exists")
    _add_key_args(enc)
    enc.set_defaults(func=cmd_encrypt)

    dec = sub.add_parser("decrypt", help="decrypt an encrypted SQLite db into plaintext")
    dec.add_argument("source", help="encrypted .db to read")
    dec.add_argument("dest", help="plaintext .db to create")
    dec.add_argument("--force", action="store_true", help="overwrite destination if it exists")
    _add_key_args(dec)
    dec.set_defaults(func=cmd_decrypt)

    ver = sub.add_parser("verify", help="try to open an encrypted db and report OK/FAIL")
    ver.add_argument("source", help="encrypted .db to verify")
    _add_key_args(ver)
    ver.set_defaults(func=cmd_verify)

    rot = sub.add_parser("rotate", help="change the key of an encrypted db in place")
    rot.add_argument("source", help="encrypted .db to rekey")
    og = rot.add_mutually_exclusive_group()
    og.add_argument("--old-key-env", dest="old_key_env", metavar="VAR")
    og.add_argument("--old-key-file", dest="old_key_file", metavar="PATH")
    ng = rot.add_mutually_exclusive_group()
    ng.add_argument("--new-key-env", dest="new_key_env", metavar="VAR")
    ng.add_argument("--new-key-file", dest="new_key_file", metavar="PATH")
    rot.set_defaults(func=cmd_rotate)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
