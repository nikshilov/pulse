import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import import_cleaned_chats  # noqa: E402


MIGRATIONS = Path(__file__).resolve().parents[2] / "internal" / "store" / "migrations"


def _apply_migrations(db_path: Path) -> None:
    con = sqlite3.connect(db_path)
    for mig in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(mig.read_text())
    con.commit()
    con.close()


def test_iter_observations_chunks_cleaned_markdown(tmp_path):
    root = tmp_path / "cleaned"
    root.mkdir()
    chat = root / "session.md"
    chat.write_text(
        "\n".join([
            "# Cleaned chat: session.jsonl",
            "",
            "## 1. User -- 2026-04-25T01:00:00Z",
            "",
            "remember Garden boundary",
            "",
            "## 2. Assistant -- 2026-04-25T01:01:00Z",
            "",
            "I will keep the boundary explicit.",
        ]),
        encoding="utf-8",
    )

    observations = import_cleaned_chats.iter_observations(root, max_chars=80)

    assert len(observations) == 2
    assert observations[0]["source_kind"] == "claude_cleaned_md"
    assert observations[0]["actors"] == [{"kind": "user", "id": "nik"}]
    assert observations[1]["actors"] == [{"kind": "assistant", "id": "claude"}]
    assert observations[0]["metadata"]["chunk_count"] == 2


def test_import_observations_inserts_jobs_and_dedupes(tmp_path):
    db = tmp_path / "pulse.db"
    _apply_migrations(db)
    root = tmp_path / "cleaned"
    root.mkdir()
    (root / "session.md").write_text(
        "## 1. User — 2026-04-25T01:00:00Z\n\nremember Sonya boundary\n",
        encoding="utf-8",
    )
    observations = import_cleaned_chats.iter_observations(root, max_chars=12000)

    first = import_cleaned_chats.import_observations(str(db), observations)
    second = import_cleaned_chats.import_observations(str(db), observations)

    assert first["inserted"] == 1
    assert first["jobs"] == 1
    assert second["duplicates"] == 1

    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM extraction_jobs WHERE state='pending'").fetchone()[0] == 1
    con.close()
