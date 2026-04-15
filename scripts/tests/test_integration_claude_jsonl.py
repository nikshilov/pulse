"""End-to-end: spin up Pulse, ingest fixture JSONL, verify DB rows.

Requires: `bin/pulse` (run `go build -o bin/pulse ./cmd/pulse`).
Marked slow — skipped unless PULSE_E2E=1.
"""

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
import httpx

PULSE_BIN = Path(__file__).resolve().parent.parent.parent / "bin" / "pulse"
FIXTURE = Path(__file__).parent / "fixtures" / "claude_jsonl_sample.jsonl"
CLI = Path(__file__).resolve().parent.parent / "pulse_ingest.py"

pytestmark = pytest.mark.skipif(os.getenv("PULSE_E2E") != "1", reason="set PULSE_E2E=1 to run")


def _wait_for_health(base_url: str, key: str, attempts: int = 30) -> None:
    for _ in range(attempts):
        try:
            r = httpx.get(f"{base_url}/health", headers={"X-Pulse-Key": key}, timeout=0.5)
            if r.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.3)
    raise RuntimeError("pulse did not become healthy in time")


def test_ingest_e2e(tmp_path):
    assert PULSE_BIN.exists(), f"build first: go build -o {PULSE_BIN} ./cmd/pulse"

    data_dir = tmp_path / "pulse-data"
    data_dir.mkdir()
    base_url = "http://127.0.0.1:18899"

    # soul.md is required by prompt.NewBuilder; write a minimal stub
    (data_dir / "soul.md").write_text("# test\nYou are a test assistant.\n")

    env = {**os.environ, "ANTHROPIC_API_KEY": "test-stub"}
    proc = subprocess.Popen(
        [str(PULSE_BIN), "-data-dir", str(data_dir), "-addr", "127.0.0.1:18899"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        # Wait for secret.key to be created, then read it
        secret_file = data_dir / "secret.key"
        for _ in range(30):
            if secret_file.exists():
                break
            time.sleep(0.2)
        assert secret_file.exists(), "secret.key was not created"
        secret = secret_file.read_text().strip()

        _wait_for_health(base_url, secret)

        # First ingest
        result = subprocess.run(
            [sys.executable, str(CLI),
             "--source=claude-jsonl", f"--path={FIXTURE}",
             f"--pulse-url={base_url}", "--batch-size=10",
             f"--pulse-key={secret}"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"ingest failed: stdout={result.stdout!r} stderr={result.stderr!r}"

        con = sqlite3.connect(data_dir / "pulse.db")
        try:
            count = con.execute(
                "SELECT COUNT(*) FROM observations WHERE source_kind='claude_jsonl'"
            ).fetchone()[0]
            assert count == 2, f"expected 2 rows, got {count}"
        finally:
            con.close()

        # Second ingest — expect duplicates=2
        result2 = subprocess.run(
            [sys.executable, str(CLI),
             "--source=claude-jsonl", f"--path={FIXTURE}",
             f"--pulse-url={base_url}", "--batch-size=10",
             f"--pulse-key={secret}"],
            capture_output=True, text=True,
        )
        assert result2.returncode == 0
        assert "duplicates=2" in result2.stdout, f"got stdout: {result2.stdout!r}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
