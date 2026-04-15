import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import sqlite3
import pytest
from unittest.mock import patch
import pulse_extract


@pytest.fixture
def seeded_db(tmp_path):
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    # Apply all migrations
    for mig in sorted(Path(__file__).resolve().parents[2].glob("internal/store/migrations/*.sql")):
        con.executescript(mig.read_text())
    con.commit()

    con.execute("""INSERT INTO observations
        (source_kind, source_id, content_hash, version, scope, captured_at, observed_at, actors, content_text, metadata, raw_json)
        VALUES ('claude_jsonl','f:1','h1',1,'shared','2026-04-15T00:00:00Z','2026-04-15T00:00:01Z','[{"kind":"user","id":"nik"}]','Аня упомянула Федю — пошёл в школу','{}','{}')""")

    con.execute("""INSERT INTO extraction_jobs
        (observation_ids, state, attempts, created_at, updated_at)
        VALUES ('[1]', 'pending', 0, '2026-04-15T00:00:00Z', '2026-04-15T00:00:00Z')""")
    con.commit()
    con.close()
    return db


def test_extract_loop_processes_pending(seeded_db, monkeypatch):
    from unittest.mock import MagicMock

    fake_triage = MagicMock(return_value=[{"verdict": "extract", "reason": "family mention"}])
    fake_extract = MagicMock(return_value={
        "entities": [{"canonical_name": "Anna", "kind": "person", "aliases": ["Аня"], "salience": 0.9, "emotional_weight": 0.7}],
        "relations": [],
        "events": [],
        "facts": [],
        "merge_candidates": [],
    })

    monkeypatch.setattr(pulse_extract, "call_sonnet_triage", fake_triage)
    monkeypatch.setattr(pulse_extract, "call_opus_extract", fake_extract)

    rc = pulse_extract.run_once(str(seeded_db), budget_usd_remaining=10.0)
    assert rc == 0

    con = sqlite3.connect(seeded_db)
    entities = con.execute("SELECT canonical_name FROM entities").fetchall()
    assert any(e[0] == "Anna" for e in entities)

    # Job marked done
    state = con.execute("SELECT state FROM extraction_jobs WHERE id=1").fetchone()[0]
    assert state == "done"


def test_failed_job_moves_to_dlq_after_three_attempts(seeded_db, monkeypatch):
    import pulse_extract

    def boom(*_a, **_kw):
        raise RuntimeError("triage API down")

    monkeypatch.setattr(pulse_extract, "call_sonnet_triage", boom)

    for _ in range(3):
        pulse_extract.run_once(str(seeded_db))

    con = sqlite3.connect(seeded_db)
    state = con.execute("SELECT state FROM extraction_jobs WHERE id=1").fetchone()[0]
    assert state == "dlq"


def test_budget_exhausted_skips_extraction(seeded_db, capsys):
    import pulse_extract
    rc = pulse_extract.run_once(str(seeded_db), budget_usd_remaining=0.0)
    captured = capsys.readouterr()
    assert "budget" in captured.out.lower()

    con = sqlite3.connect(seeded_db)
    # Job should remain pending, NOT be moved to running/failed
    state = con.execute("SELECT state FROM extraction_jobs WHERE id=1").fetchone()[0]
    assert state == "pending"


def test_relations_and_facts_persisted(seeded_db, monkeypatch):
    from unittest.mock import MagicMock
    import sqlite3
    import pulse_extract

    fake_triage = MagicMock(return_value=[{"verdict": "extract", "reason": "family"}])
    fake_extract = MagicMock(return_value={
        "entities": [
            {"canonical_name": "Anna", "kind": "person", "aliases": ["Аня"], "salience": 0.9, "emotional_weight": 0.8},
            {"canonical_name": "Fedya", "kind": "person", "aliases": ["Федя"], "salience": 0.5, "emotional_weight": 0.3},
        ],
        "relations": [
            {"from": "Anna", "to": "Fedya", "kind": "parent", "strength": 0.9},
        ],
        "facts": [
            {"entity": "Anna", "text": "Anna is Fedya's mother", "confidence": 0.95},
            {"entity": "Fedya", "text": "Fedya started school in 2025", "confidence": 0.8},
        ],
        "events": [],
        "merge_candidates": [],
    })
    monkeypatch.setattr(pulse_extract, "call_sonnet_triage", fake_triage)
    monkeypatch.setattr(pulse_extract, "call_opus_extract", fake_extract)

    pulse_extract.run_once(str(seeded_db))

    con = sqlite3.connect(seeded_db)
    # Relation resolved and stored
    rel = con.execute("SELECT from_entity_id, to_entity_id, kind, strength FROM relations").fetchone()
    assert rel is not None, "no relation persisted"
    anna_id = con.execute("SELECT id FROM entities WHERE canonical_name='Anna'").fetchone()[0]
    fedya_id = con.execute("SELECT id FROM entities WHERE canonical_name='Fedya'").fetchone()[0]
    assert rel[0] == anna_id
    assert rel[1] == fedya_id
    assert rel[2] == "parent"
    assert rel[3] == 0.9

    # Facts resolved to entities
    facts = con.execute("SELECT entity_id, text, confidence FROM facts ORDER BY id").fetchall()
    assert len(facts) == 2
    assert facts[0][0] == anna_id
    assert facts[0][1] == "Anna is Fedya's mother"
    assert facts[1][0] == fedya_id


def test_unknown_entity_reference_is_skipped(seeded_db, monkeypatch, capsys):
    from unittest.mock import MagicMock
    import sqlite3
    import pulse_extract

    fake_triage = MagicMock(return_value=[{"verdict": "extract", "reason": "family"}])
    fake_extract = MagicMock(return_value={
        "entities": [
            {"canonical_name": "Anna", "kind": "person", "aliases": [], "salience": 0.9, "emotional_weight": 0.8},
        ],
        "relations": [
            {"from": "Anna", "to": "Ghost", "kind": "knows", "strength": 0.5},
        ],
        "facts": [
            {"entity": "Nobody", "text": "irrelevant", "confidence": 0.9},
        ],
        "events": [],
        "merge_candidates": [],
    })
    monkeypatch.setattr(pulse_extract, "call_sonnet_triage", fake_triage)
    monkeypatch.setattr(pulse_extract, "call_opus_extract", fake_extract)

    pulse_extract.run_once(str(seeded_db))

    con = sqlite3.connect(seeded_db)
    # No relation or fact because both referenced unknown entities
    assert con.execute("SELECT COUNT(*) FROM relations").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0
    # But Anna itself was still persisted
    assert con.execute("SELECT COUNT(*) FROM entities WHERE canonical_name='Anna'").fetchone()[0] == 1

    captured = capsys.readouterr()
    assert "skipping relation" in captured.out.lower() or "unknown entity" in captured.out.lower()
