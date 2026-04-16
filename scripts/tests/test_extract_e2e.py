import json
import sqlite3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pulse_extract
from extract import prompts

FIXTURES = Path(__file__).parent / "fixtures" / "extract_responses.json"


def _seed(tmp_path):
    db = tmp_path / "e2e.db"
    con = sqlite3.connect(db)
    for mig in sorted(Path(__file__).resolve().parents[2].glob("internal/store/migrations/*.sql")):
        con.executescript(mig.read_text())
    con.commit()

    con.execute("""INSERT INTO observations
        (source_kind, source_id, content_hash, version, scope, captured_at, observed_at, actors, content_text, metadata, raw_json)
        VALUES ('claude_jsonl','f:1','h1',1,'shared','2026-04-15T00:00:00Z','2026-04-15T00:00:00Z',
                '[{"kind":"user","id":"nik"}]', 'Аня сказала что Федя пошёл в школу', '{}', '{}')""")
    con.execute("""INSERT INTO observations
        (source_kind, source_id, content_hash, version, scope, captured_at, observed_at, actors, content_text, metadata, raw_json)
        VALUES ('claude_jsonl','f:2','h2',1,'shared','2026-04-15T00:00:00Z','2026-04-15T00:00:00Z',
                '[{"kind":"user","id":"nik"}]', 'привет', '{}', '{}')""")
    con.execute("""INSERT INTO extraction_jobs
        (observation_ids, state, attempts, created_at, updated_at)
        VALUES ('[1,2]', 'pending', 0, '2026-04-15T00:00:00Z', '2026-04-15T00:00:00Z')""")
    con.commit()
    con.close()
    return db


def test_e2e_extraction_creates_graph(tmp_path, monkeypatch):
    fixtures = json.loads(FIXTURES.read_text())
    db = _seed(tmp_path)

    def fake_triage(*_args, **_kwargs):
        return prompts.parse_triage_response(fixtures["triage"], expected_count=2)

    def fake_extract(_prompt):
        return fixtures["extract_1"]

    monkeypatch.setattr(pulse_extract, "call_sonnet_triage", fake_triage)
    monkeypatch.setattr(pulse_extract, "call_opus_extract", fake_extract)

    rc = pulse_extract.run_once(str(db))
    assert rc == 0

    con = sqlite3.connect(db)
    names = {r[0] for r in con.execute("SELECT canonical_name FROM entities")}
    assert "Anna" in names
    assert "Fedya" in names

    ev_count = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert ev_count == 1

    # evidence linked to obs 1 (the extracted one), not 2 (skipped)
    ev_obs = {r[0] for r in con.execute("SELECT DISTINCT observation_id FROM evidence")}
    assert ev_obs == {1}

    job_state = con.execute("SELECT state FROM extraction_jobs WHERE id=1").fetchone()[0]
    assert job_state == "done"


def test_e2e_prints_apply_report(tmp_path, monkeypatch, capsys):
    fixtures = json.loads(FIXTURES.read_text())
    db = _seed(tmp_path)

    def fake_triage(*_args, **_kwargs):
        return prompts.parse_triage_response(fixtures["triage"], expected_count=2)

    def fake_extract(_prompt):
        return fixtures["extract_1"]

    monkeypatch.setattr(pulse_extract, "call_sonnet_triage", fake_triage)
    monkeypatch.setattr(pulse_extract, "call_opus_extract", fake_extract)

    rc = pulse_extract.run_once(str(db))
    assert rc == 0

    captured = capsys.readouterr()
    assert "apply_report=" in captured.out
    # Sanity: the report must mention at least one entity_written
    assert '"entities_written"' in captured.out
