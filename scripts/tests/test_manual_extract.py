import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import pulse_manual_extract  # noqa: E402


MIGRATIONS = Path(__file__).resolve().parents[2] / "internal" / "store" / "migrations"


def _apply_migrations(db_path: Path) -> None:
    con = sqlite3.connect(db_path)
    for mig in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(mig.read_text())
    con.commit()
    con.close()


def _seed_db(tmp_path: Path) -> Path:
    db = tmp_path / "manual.db"
    _apply_migrations(db)
    con = sqlite3.connect(db)
    con.execute(
        """INSERT INTO observations
           (id, source_kind, source_id, content_hash, version, scope, captured_at,
            observed_at, actors, content_text, metadata, raw_json)
           VALUES (1, 'claude_jsonl', 'f:1', 'h1', 1, 'shared',
                   '2026-04-23T00:00:00Z', '2026-04-23T00:00:00Z',
                   '[{"kind":"user","id":"nik"}]',
                   'Garden includes Mirror guardrails for Nik.',
                   '{"cwd":"/x/Garden"}', '{}')"""
    )
    con.execute(
        """INSERT INTO extraction_jobs
           (id, observation_ids, state, attempts, created_at, updated_at)
           VALUES (1, '[1]', 'pending', 0,
                   '2026-04-23T00:00:00Z', '2026-04-23T00:00:00Z')"""
    )
    con.commit()
    con.close()
    return db


def _filled_batch() -> dict:
    return {
        "schema": pulse_manual_extract.SCHEMA,
        "model": "codex-test",
        "observations": [
            {
                "obs_id": 1,
                "job_id": 1,
                "triage": {"verdict": "extract", "reason": "high-signal memory"},
                "extraction": {
                    "entities": [
                        {"canonical_name": "Nik Shilov", "kind": "person", "aliases": ["Nik"]},
                        {"canonical_name": "Garden", "kind": "project"},
                        {"canonical_name": "Mirror Guardrails", "kind": "project"},
                    ],
                    "relations": [
                        {"from": "Garden", "to": "Mirror Guardrails", "kind": "includes", "strength": 0.9},
                    ],
                    "events": [
                        {
                            "title": "Garden gets Mirror guardrails",
                            "description": "Nik's Garden memory system includes a protected Mirror layer.",
                            "sentiment": 0.5,
                            "emotional_weight": 0.8,
                            "ts": "2026-04-23T00:00:00Z",
                            "entities_involved": ["Nik Shilov", "Garden", "Mirror Guardrails"],
                        }
                    ],
                    "facts": [
                        {
                            "entity": "Garden",
                            "text": "Garden includes Mirror guardrails.",
                            "confidence": 0.9,
                        }
                    ],
                },
                "event_emotions": [
                    {
                        "event_title": "Garden gets Mirror guardrails",
                        "joy": 0.4,
                        "trust": 0.8,
                        "anticipation": 0.6,
                        "confidence": 0.9,
                    }
                ],
                "event_chains": [],
            }
        ],
    }


def test_prepare_batch_exports_template(tmp_path):
    db = _seed_db(tmp_path)
    payload = pulse_manual_extract.prepare_batch(str(db), ids=[1])

    assert payload["schema"] == pulse_manual_extract.SCHEMA
    assert len(payload["observations"]) == 1
    obs = payload["observations"][0]
    assert obs["obs_id"] == 1
    assert obs["job_id"] == 1
    assert obs["triage"]["verdict"] == "extract"
    assert obs["extraction"]["entities"] == []
    assert obs["metadata"]["cwd"] == "/x/Garden"


def test_apply_batch_creates_graph_artifacts_and_embeddings(tmp_path):
    db = _seed_db(tmp_path)
    summary = pulse_manual_extract.apply_batch(
        str(db),
        _filled_batch(),
        model="codex-test",
        fake_embeddings=True,
    )

    assert summary["applied"] == 1
    assert summary["fake_embeddings_written"] == 1
    assert summary["event_emotions_written"] == 1

    con = sqlite3.connect(db)
    names = {row[0] for row in con.execute("SELECT canonical_name FROM entities")}
    assert {"Nik Shilov", "Garden", "Mirror Guardrails"} <= names
    assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM evidence WHERE observation_id=1").fetchone()[0] > 0
    assert con.execute("SELECT COUNT(*) FROM graph_snapshots WHERE observation_id=1").fetchone()[0] > 0
    assert con.execute("SELECT COUNT(*) FROM event_embeddings").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM event_emotions").fetchone()[0] == 1
    assert con.execute("SELECT state FROM extraction_jobs WHERE id=1").fetchone()[0] == "done"
    models = {row[0] for row in con.execute("SELECT model FROM extraction_artifacts")}
    assert models == {"codex-test"}
    con.close()


def test_apply_batch_rejects_unknown_entity_reference(tmp_path):
    db = _seed_db(tmp_path)
    batch = _filled_batch()
    batch["observations"][0]["extraction"]["relations"][0]["to"] = "Missing Entity"

    with pytest.raises(ValueError, match="unknown to entity"):
        pulse_manual_extract.apply_batch(str(db), batch)

    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 0
    assert con.execute("SELECT state FROM extraction_jobs WHERE id=1").fetchone()[0] == "pending"
    con.close()


def test_apply_batch_can_skip_observation(tmp_path):
    db = _seed_db(tmp_path)
    batch = {
        "schema": pulse_manual_extract.SCHEMA,
        "observations": [
            {
                "obs_id": 1,
                "job_id": 1,
                "triage": {"verdict": "skip", "reason": "not useful"},
                "extraction": {"entities": [], "relations": [], "events": [], "facts": []},
            }
        ],
    }

    summary = pulse_manual_extract.apply_batch(str(db), batch, model="codex-test")
    assert summary["skipped"] == 1

    con = sqlite3.connect(db)
    assert con.execute("SELECT state FROM extraction_jobs WHERE id=1").fetchone()[0] == "done"
    assert con.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 0
    con.close()
