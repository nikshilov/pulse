import subprocess
import sys
from pathlib import Path

CLI = Path(__file__).parent.parent / "pulse_ingest.py"


def test_help_shows_sources():
    result = subprocess.run(
        [sys.executable, str(CLI), "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "--source" in result.stdout
    assert "claude-jsonl" in result.stdout
