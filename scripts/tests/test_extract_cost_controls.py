"""Tests for extraction cost controls (2026-04 FinOps pass).

Covers:
- cost_usd written to extraction_metrics on every insert
- live budget gate (pre-flight + mid-batch)
- top-K candidate-entity selection for the Opus prompt
- cache_control markers on the static prefix of Opus/Sonnet calls
"""

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import pulse_extract  # noqa: E402

MIGRATIONS = Path(__file__).resolve().parents[2] / "internal" / "store" / "migrations"
MOCK_USAGE = {"input_tokens": 0, "output_tokens": 0, "model": "test"}


def _apply_migrations(db_path: Path) -> None:
    con = sqlite3.connect(db_path)
    for mig in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(mig.read_text())
    con.commit()
    con.close()


def _seed_one_pending_job(db: Path) -> None:
    """Observation + one pending extraction_job referencing it."""
    con = sqlite3.connect(db)
    con.execute(
        """INSERT INTO observations
           (source_kind,source_id,content_hash,version,scope,captured_at,observed_at,
            actors,content_text,metadata,raw_json)
           VALUES ('claude_jsonl','f:1','h1',1,'shared','2026-04-16T00:00:00Z',
                   '2026-04-16T00:00:01Z','[{"kind":"user","id":"nik"}]',
                   'Anna mentioned Fedya at school','{}','{}')"""
    )
    con.execute(
        """INSERT INTO extraction_jobs
           (observation_ids,state,attempts,created_at,updated_at)
           VALUES ('[1]','pending',0,'2026-04-16T00:00:00Z','2026-04-16T00:00:00Z')"""
    )
    con.commit()
    con.close()


# ---------- cost_usd ----------


def test_cost_usd_written_to_metrics(tmp_path):
    """_save_metrics must compute and persist cost_usd for a known model."""
    db = tmp_path / "c.db"
    _apply_migrations(db)

    # Seed one job row so the FK in extraction_metrics is satisfied.
    con = sqlite3.connect(db)
    con.execute(
        """INSERT INTO extraction_jobs
           (observation_ids,state,attempts,created_at,updated_at)
           VALUES ('[]','pending',0,'2026-04-16T00:00:00Z','2026-04-16T00:00:00Z')"""
    )
    con.commit()
    con.close()

    con = pulse_extract._open_connection(str(db))
    try:
        con.execute("BEGIN IMMEDIATE")
        pulse_extract._save_metrics(
            con, job_id=1,
            usage={
                "model": "claude-opus-4-6",
                "input_tokens": 1000,
                "output_tokens": 500,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        )
        con.execute("COMMIT")

        row = con.execute(
            "SELECT model, cost_usd FROM extraction_metrics WHERE job_id=1"
        ).fetchone()
    finally:
        con.close()

    assert row is not None
    assert row[0] == "claude-opus-4-6"
    # 1000 * 15 / 1M + 500 * 75 / 1M = 0.015 + 0.0375 = 0.0525
    assert row[1] == pytest.approx(0.0525, rel=1e-6)


def test_compute_cost_usd_handles_cache_tokens():
    """Cache-write is 1.25×, cache-read is 0.10× the input rate."""
    cost = pulse_extract._compute_cost_usd({
        "model": "claude-opus-4-6",
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 1_000_000,  # 1M tokens
        "cache_read_input_tokens": 1_000_000,
    })
    # $15 * 1.25 + $15 * 0.10 = $18.75 + $1.50 = $20.25
    assert cost == pytest.approx(20.25, rel=1e-6)


def test_compute_cost_usd_unknown_model_returns_zero():
    assert pulse_extract._compute_cost_usd({"model": "gpt-5"}) == 0.0
    assert pulse_extract._compute_cost_usd({}) == 0.0


# ---------- budget gate ----------


def test_budget_gate_aborts_when_today_spend_over_budget(tmp_path, monkeypatch):
    """If today's metrics sum > budget, abort BEFORE any Anthropic call."""
    db = tmp_path / "b.db"
    _apply_migrations(db)
    _seed_one_pending_job(db)

    # Seed $12 of today's spend.
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO extraction_metrics (job_id, model, input_tokens, output_tokens, cost_usd) "
        "VALUES (1, 'claude-opus-4-6', 0, 0, 12.0)"
    )
    con.commit()
    con.close()

    def boom(*_a, **_kw):
        raise AssertionError("Anthropic must NOT be called when over budget")

    monkeypatch.setattr(pulse_extract, "call_sonnet_triage", boom)
    monkeypatch.setattr(pulse_extract, "call_opus_extract", boom)

    rc = pulse_extract.run_once(str(db), budget_usd_remaining=10.0)
    assert rc == 0

    # Job stays pending (no state change).
    con = sqlite3.connect(db)
    state = con.execute("SELECT state FROM extraction_jobs WHERE id=1").fetchone()[0]
    con.close()
    assert state == "pending"


def test_budget_gate_allows_when_under_budget(tmp_path, monkeypatch):
    """$2 spend, $10 budget → normal processing continues."""
    db = tmp_path / "b.db"
    _apply_migrations(db)
    _seed_one_pending_job(db)

    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO extraction_metrics (job_id, model, input_tokens, output_tokens, cost_usd) "
        "VALUES (1, 'claude-opus-4-6', 0, 0, 2.0)"
    )
    con.commit()
    con.close()

    fake_triage = MagicMock(return_value=([{"verdict": "skip"}], MOCK_USAGE))
    fake_extract = MagicMock(return_value=({
        "entities": [], "events": [], "relations": [], "facts": [], "merge_candidates": [],
    }, MOCK_USAGE))
    monkeypatch.setattr(pulse_extract, "call_sonnet_triage", fake_triage)
    monkeypatch.setattr(pulse_extract, "call_opus_extract", fake_extract)

    rc = pulse_extract.run_once(str(db), budget_usd_remaining=10.0)
    assert rc == 0

    # Triage WAS called (budget allowed it through).
    assert fake_triage.called
    con = sqlite3.connect(db)
    state = con.execute("SELECT state FROM extraction_jobs WHERE id=1").fetchone()[0]
    con.close()
    assert state == "done"  # all-skip triage → done


def test_budget_gate_fires_mid_batch(tmp_path, monkeypatch):
    """If the first job pushes us over budget, subsequent jobs must be skipped."""
    db = tmp_path / "b.db"
    _apply_migrations(db)

    # Two pending jobs, two observations.
    con = sqlite3.connect(db)
    for i in (1, 2):
        con.execute(
            """INSERT INTO observations
               (source_kind,source_id,content_hash,version,scope,captured_at,observed_at,
                actors,content_text,metadata,raw_json)
               VALUES ('claude_jsonl',?,?,1,'shared','2026-04-16T00:00:00Z',
                       '2026-04-16T00:00:01Z','[]',?,'{}','{}')""",
            (f"f:{i}", f"h{i}", f"obs text {i}"),
        )
        con.execute(
            """INSERT INTO extraction_jobs
               (observation_ids,state,attempts,created_at,updated_at)
               VALUES (?,'pending',0,?,?)""",
            (f"[{i}]", f"2026-04-16T00:00:0{i}Z", f"2026-04-16T00:00:0{i}Z"),
        )
    con.commit()
    con.close()

    triage_calls = {"n": 0}

    def fake_triage(_prompt, expected_count, **_kw):
        triage_calls["n"] += 1
        # After the first call, write enough spend to trip the gate.
        return ([{"verdict": "skip"}], {
            "model": "claude-opus-4-6", "input_tokens": 1_000_000, "output_tokens": 0,
        })

    monkeypatch.setattr(pulse_extract, "call_sonnet_triage", fake_triage)
    monkeypatch.setattr(pulse_extract, "call_opus_extract",
                        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("no extract expected")))

    # Budget = $5, one triage burns ~$15 → second job must be gated out.
    pulse_extract.run_once(str(db), budget_usd_remaining=5.0)
    assert triage_calls["n"] == 1, "second job should have been blocked by mid-batch gate"


# ---------- top-K candidate entities ----------


def test_top_k_candidate_entities_limits_size(tmp_path):
    """200 entities seeded → _load_candidate_entities caps at 50."""
    db = tmp_path / "k.db"
    _apply_migrations(db)

    con = sqlite3.connect(db)
    for i in range(200):
        con.execute(
            "INSERT INTO entities (canonical_name, kind, aliases, first_seen, last_seen, "
            "salience_score, emotional_weight) VALUES (?, 'person', '[]', ?, ?, ?, 0)",
            (f"Person_{i}", "2026-04-16T00:00:00Z", "2026-04-16T00:00:00Z", i / 200.0),
        )
    con.commit()

    out = pulse_extract._load_candidate_entities(
        con, {"content_text": "random text with no matches", "actors": []}, top_k=50
    )
    con.close()
    assert len(out) == 50


def test_top_k_candidate_entities_matches_tokens(tmp_path):
    """Entity whose name matches an observation token must rank above unrelated."""
    db = tmp_path / "k.db"
    _apply_migrations(db)

    con = sqlite3.connect(db)
    # 10 entities with increasing salience; only "Fedya" matches our observation.
    for i, name in enumerate([
        "Alpha", "Beta", "Gamma", "Delta", "Epsilon",
        "Zeta", "Eta", "Theta", "Iota", "Fedya",
    ]):
        con.execute(
            "INSERT INTO entities (canonical_name, kind, aliases, first_seen, last_seen, "
            "salience_score, emotional_weight) VALUES (?, 'person', '[]', ?, ?, ?, 0)",
            (name, "2026-04-16T00:00:00Z", "2026-04-16T00:00:00Z", i * 0.05),
        )
    con.commit()

    out = pulse_extract._load_candidate_entities(
        con, {"content_text": "Anna mentioned Fedya at school", "actors": []}, top_k=5
    )
    con.close()

    # Fedya should be in the result, and should be the first (matched first, rest pad).
    names = [e["canonical_name"] for e in out]
    assert "Fedya" in names
    assert names[0] == "Fedya", f"matched entity must rank first; got {names}"


def test_top_k_candidate_entities_matches_actor_id(tmp_path):
    """Entity whose canonical_name matches an observation.actors[*].id is treated as matched."""
    db = tmp_path / "k.db"
    _apply_migrations(db)

    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO entities (canonical_name, kind, aliases, first_seen, last_seen, "
        "salience_score, emotional_weight) VALUES ('nik', 'person', '[]', "
        "'2026-04-16T00:00:00Z', '2026-04-16T00:00:00Z', 0.1, 0)"
    )
    con.execute(
        "INSERT INTO entities (canonical_name, kind, aliases, first_seen, last_seen, "
        "salience_score, emotional_weight) VALUES ('Unrelated', 'person', '[]', "
        "'2026-04-16T00:00:00Z', '2026-04-16T00:00:00Z', 0.9, 0)"
    )
    con.commit()

    out = pulse_extract._load_candidate_entities(
        con, {"content_text": "", "actors": [{"kind": "user", "id": "nik"}]}, top_k=2
    )
    con.close()
    assert [e["canonical_name"] for e in out][0] == "nik"


# ---------- prompt caching (cache_control on static prefix) ----------


def test_call_opus_extract_uses_cache_control_on_static_prefix(monkeypatch):
    """Static prefix must be marked ephemeral; dynamic suffix must not be."""
    fake_block = MagicMock()
    fake_block.type = "tool_use"
    fake_block.name = "save_extraction"
    fake_block.input = {"entities": [], "relations": [], "events": [], "facts": []}

    fake_msg = MagicMock()
    fake_msg.content = [fake_block]
    fake_msg.usage.input_tokens = 100
    fake_msg.usage.output_tokens = 50
    fake_msg.usage.cache_creation_input_tokens = 0
    fake_msg.usage.cache_read_input_tokens = 0

    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_msg

    monkeypatch.setattr(pulse_extract, "_anthropic_client", lambda: fake_client)

    pulse_extract.call_opus_extract("STATIC PREFIX", dynamic_suffix="DYNAMIC TAIL")

    _, kwargs = fake_client.messages.create.call_args
    content = kwargs["messages"][0]["content"]
    assert isinstance(content, list)
    assert len(content) == 2
    assert content[0]["text"] == "STATIC PREFIX"
    assert content[0].get("cache_control") == {"type": "ephemeral"}
    assert content[1]["text"] == "DYNAMIC TAIL"
    assert "cache_control" not in content[1]

    # Tool definition also carries cache_control.
    assert kwargs["tools"][0]["cache_control"] == {"type": "ephemeral"}


def test_call_sonnet_triage_uses_cache_control_on_static_prefix(monkeypatch):
    fake_block = MagicMock()
    fake_block.type = "tool_use"
    fake_block.name = "triage_observations"
    fake_block.input = {"verdicts": [{"index": 1, "verdict": "skip", "reason": "x"}]}

    fake_msg = MagicMock()
    fake_msg.content = [fake_block]
    fake_msg.usage.input_tokens = 10
    fake_msg.usage.output_tokens = 5
    fake_msg.usage.cache_creation_input_tokens = 0
    fake_msg.usage.cache_read_input_tokens = 0

    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_msg

    monkeypatch.setattr(pulse_extract, "_anthropic_client", lambda: fake_client)

    pulse_extract.call_sonnet_triage(
        "STATIC", expected_count=1, dynamic_suffix="DYN",
    )

    _, kwargs = fake_client.messages.create.call_args
    content = kwargs["messages"][0]["content"]
    assert content[0]["cache_control"] == {"type": "ephemeral"}
    assert content[0]["text"] == "STATIC"
    assert content[1]["text"] == "DYN"


def test_build_extract_prompt_parts_splits_static_and_dynamic():
    from extract.prompts import build_extract_prompt_parts, EXTRACT_INSTRUCTIONS

    obs = {"source_kind": "telegram", "actors": [], "content_text": "Hello"}
    ctx = {"existing_entities": [
        {"id": 1, "canonical_name": "Anna", "kind": "person", "aliases": ["Аня"]}
    ]}
    static, dynamic = build_extract_prompt_parts(obs, ctx)
    # Static carries the immutable instructions.
    assert EXTRACT_INSTRUCTIONS.strip() in static
    # existing_entities block is in the DYNAMIC half (varies with top-K).
    assert "id=1 name=Anna" in dynamic
    assert "Hello" in dynamic
    # The observation body must not leak into static.
    assert "Hello" not in static
