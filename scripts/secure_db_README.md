# secure_db.py — at-rest encryption for Pulse SQLite databases

A small, GPG-style CLI: encrypt a Pulse database file, decrypt it, verify
a key opens it, rotate the key. Implemented with SQLCipher.

## What this is for

- Encrypt `pulse.db` so that reading the file without the key returns
  gibberish. Protects against:
  - any local process that gets file-read access (Spotlight indexer,
    backup utility, rogue script, accidental git add)
  - your laptop getting imaged when it's unlocked
  - a .db ending up in a Dropbox-like sync that doesn't respect Mac
    FileVault
- Complements FileVault. Does **not** replace it.

## What this is NOT for

- **`~/OpenClawWorkspace/persistent/` and `.claude/projects/*/memory/MEMORY.md`
  are out of scope.** They live in a different repo and contain
  intimate therapy-level material. Encrypting those requires a
  different approach (e.g. `gpg -c` on the whole directory before
  stepping away, or switching to Keybase-managed files). A separate
  tool.
- Runtime (in-RAM) protection. While Pulse is running and the db is
  decrypted, memory pages are readable by any process with the same
  uid. This tool only addresses at-rest.
- A substitute for `_open_connection` refactoring. The Pulse codebase
  still uses `sqlite3` stdlib and reads plaintext. If you want SQLCipher
  transparent to the app, you have to replace `sqlite3` with
  `pysqlcipher3` everywhere. Out of scope here.

## Setup

```bash
# One-time: install the optional SQLCipher binding
pip install pysqlcipher3

# Pick a key. Long. Store it somewhere separate from the DB.
export PULSE_DB_KEY="$(pwgen 40 1)"   # or `openssl rand -base64 32`
```

On macOS the recommended place to store the key is the Keychain:

```bash
# save
security add-generic-password -a pulse -s pulse-db-key -w "$PULSE_DB_KEY"

# load into env when you need it
export PULSE_DB_KEY="$(security find-generic-password -w -a pulse -s pulse-db-key)"
```

## Workflow

```bash
# Encrypt before stepping away
python scripts/secure_db.py encrypt pulse-dev/pulse.db pulse-dev/pulse.db.enc --key-env PULSE_DB_KEY
rm pulse-dev/pulse.db     # plaintext gone

# To work: decrypt first
python scripts/secure_db.py decrypt pulse-dev/pulse.db.enc pulse-dev/pulse.db --key-env PULSE_DB_KEY

# Run Pulse as usual — existing _open_connection reads plaintext pulse.db

# Before stepping away again:
python scripts/secure_db.py encrypt pulse-dev/pulse.db pulse-dev/pulse.db.enc --key-env PULSE_DB_KEY
rm pulse-dev/pulse.db
```

## Commands

```
python scripts/secure_db.py encrypt  <plain.db>   <enc.db>  [--key-env VAR | --key-file PATH] [--force]
python scripts/secure_db.py decrypt  <enc.db>     <plain.db>[--key-env VAR | --key-file PATH] [--force]
python scripts/secure_db.py verify   <enc.db>     [--key-env VAR | --key-file PATH]
python scripts/secure_db.py rotate   <enc.db>     [--old-key-env VAR | --old-key-file PATH]
                                                  [--new-key-env VAR | --new-key-file PATH]
```

### Key sourcing priority

1. `--key-env VAR` — read from environment variable. **Recommended for
   automation.**
2. `--key-file PATH` — read from file. Trailing newline is stripped.
   File should be `chmod 600`.
3. Neither — interactive prompt via `getpass.getpass()`.

All three paths feed the key into SQLCipher's `PRAGMA key = '...';` in
passphrase form. SQLCipher internally runs PBKDF2 over the passphrase
so human-typed keys are safe (though a long random key is always
better).

### Verification

After every `encrypt` and `rotate`, `secure_db.py` re-opens the
destination with the key and reads `sqlite_master` to prove the file
is valid. If it isn't, the output is deleted and the command exits
non-zero.

Standalone verify:

```bash
python scripts/secure_db.py verify pulse-dev/pulse.db.enc --key-env PULSE_DB_KEY
# exit 0 = key works, exit 1 = wrong key or corrupt db
```

### Rotation

```bash
# With two env vars in your shell:
export OLD_KEY="..."
export NEW_KEY="..."
python scripts/secure_db.py rotate pulse-dev/pulse.db.enc \
    --old-key-env OLD_KEY --new-key-env NEW_KEY
```

Rotation is in-place (`PRAGMA rekey`). If it fails mid-way the file may
be in a half-rotated state — restore from a backup. Always make a copy
before rotating:

```bash
cp pulse-dev/pulse.db.enc pulse-dev/pulse.db.enc.bak
```

## File permissions

All files written by this tool get `chmod 600` (owner read/write
only). Best-effort only — no-op on filesystems that don't support
POSIX mode bits.

## Why this design instead of transparent SQLCipher

Making Pulse transparently encrypted would require swapping `sqlite3`
for `pysqlcipher3` in:

- `scripts/pulse_extract.py::_open_connection`
- `scripts/pulse_consolidate.py::_open_connection`
- `scripts/extract/retrieval.py`
- every test file that opens the db with plain `sqlite3.connect`
- every migration

`pysqlcipher3` ships a different C binding and has meaningfully
different install requirements (OpenSSL headers). Making it a hard
dependency would complicate every contributor's setup. A
**migrate-on-demand file tool** is a much smaller change for the same
threat model (data at rest).

## Future work

- **Transparent integration** via a `DB_BACKEND` env var that picks
  between stdlib sqlite3 and pysqlcipher3. Gated on pysqlcipher3 being
  importable. Separate PR.
- **Keychain integration inside the tool** so you don't have to
  export to an env var. Would use the `keyring` package. Separate PR.
- **MEMORY.md and persistent/** — the hotter target, and the one
  flagged by the red-team judge. Not fixed here. Options:
  - `gpg -c` the directory before stepping away (manual, easy, works
    today)
  - git-crypt or age-encrypted repo (good for syncable state)
  - move to Keybase filesystem (good for cross-device)
  - a sibling script to this one that tar+gpg's the directory
  - Explore separately.

## Caveats / known issues

- `pysqlcipher3` on macOS can fail to build against the system OpenSSL
  (too old on some Pythons). Common workarounds: install via
  `LIBSQLCIPHER_PATH=/opt/homebrew/opt/sqlcipher ... pip install
  pysqlcipher3` after `brew install sqlcipher`. If you hit
  `openssl/opensslv.h: No such file or directory`, that's the
  symptom.
- SQLCipher encrypts page contents but the on-disk file has
  predictable header bytes. Don't rely on this to hide the *fact* that
  you have a SQLCipher db — it's identifiable via `file` and headers.
- PRAGMA key cannot use bound parameters; the key is embedded in SQL
  text. The tool single-quote-escapes the key. Still — avoid keys
  containing control characters or null bytes.

## Backup reminder

**Your key is the only thing standing between the encrypted file and
oblivion.** If you lose the key the data is gone. Write it down. Put
it in a password manager. Put it in Keychain AND somewhere else.
