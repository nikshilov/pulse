"""Tests for the real-corpus bench runner (scripts/bench/run_real_eval.py).

These tests verify:
  - LLM-free ingest actually builds a valid Pulse graph
  - Alex is marked is_self=1 (prerequisite for retrieval's self-anchor strip)
  - The end-to-end eval function is importable and returns metrics
"""

import json
import sys
from pathlib import Path

import pytest


# Make scripts/ importable
_TESTS = Path(__file__).resolve().parent
_SCRIPTS = _TESTS.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


CORPUS_PATH = Path.home() / "dev/ai/bench/datasets/empathic-memory-corpus.json"


# We skip if the corpus isn't present — the runner is usable by anyone with
# the bench repo checked out; CI without it should not fail here.
_CORPUS_AVAILABLE = CORPUS_PATH.exists()
skip_if_no_corpus = pytest.mark.skipif(
    not _CORPUS_AVAILABLE,
    reason=f"empathic-memory-corpus.json not found at {CORPUS_PATH}",
)


@skip_if_no_corpus
def test_ingest_corpus_creates_entities_and_events():
    """Loading + ingesting the 30-event corpus produces the expected graph
    shape: 6-10 person entities, 30 event rows, non-empty event_entities
    junctions, non-empty facts."""
    from bench.run_real_eval import fresh_db, ingest_corpus

    corpus = json.loads(CORPUS_PATH.read_text())
    con = fresh_db()
    stats = ingest_corpus(con, corpus)

    # 7 persons in PERSON_CATALOG (Alex, Maya, Sarah, Cooper, Ethan, Jordan, David)
    assert 6 <= stats["n_entities"] <= 10, stats
    assert stats["n_events"] == 30, stats
    assert stats["n_event_entities"] > 0
    assert stats["n_facts"] > 0

    # Every event that mentions a person produced at least one junction row
    n_events_with_persons = sum(
        1 for persons in stats["event_persons"].values() if persons
    )
    assert stats["n_event_entities"] >= n_events_with_persons


@skip_if_no_corpus
def test_ingest_corpus_marks_alex_as_self():
    """The real use case for is_self=1 — Alex is the user in the corpus."""
    from bench.run_real_eval import fresh_db, ingest_corpus

    corpus = json.loads(CORPUS_PATH.read_text())
    con = fresh_db()
    ingest_corpus(con, corpus)

    row = con.execute(
        "SELECT canonical_name, is_self FROM entities WHERE is_self = 1"
    ).fetchall()
    assert len(row) == 1, f"expected exactly one self-entity, got {row}"
    assert row[0][0] == "Alex"


@skip_if_no_corpus
def test_ingest_corpus_is_idempotent():
    """Running ingest twice on the same connection produces the same row
    counts — required so the runner can be re-invoked without surprise."""
    from bench.run_real_eval import fresh_db, ingest_corpus

    corpus = json.loads(CORPUS_PATH.read_text())
    con = fresh_db()
    stats1 = ingest_corpus(con, corpus)
    stats2 = ingest_corpus(con, corpus)
    assert stats1["n_entities"] == stats2["n_entities"]
    assert stats1["n_events"] == stats2["n_events"]
    assert stats1["n_event_entities"] == stats2["n_event_entities"]
    assert stats1["n_facts"] == stats2["n_facts"]


@skip_if_no_corpus
def test_events_to_entity_gt_excludes_self_when_others_present():
    """GT mapping drops Alex for events that mention other people — keeping
    him would make Recall@k trivially high."""
    from bench.run_real_eval import fresh_db, ingest_corpus, events_to_entity_gt

    corpus = json.loads(CORPUS_PATH.read_text())
    con = fresh_db()
    ingest_corpus(con, corpus)

    # Event 2: engagement to Maya — mentions Alex + Maya. GT should exclude Alex.
    gt = events_to_entity_gt(con, [2])
    # Fetch Alex's id to confirm exclusion
    alex_id = con.execute(
        "SELECT id FROM entities WHERE is_self = 1"
    ).fetchone()[0]
    assert alex_id not in gt
    # Maya should be in GT
    maya_id = con.execute(
        "SELECT id FROM entities WHERE canonical_name = 'Maya'"
    ).fetchone()[0]
    assert maya_id in gt


@skip_if_no_corpus
def test_run_real_eval_produces_metrics(capsys):
    """End-to-end: the eval function returns a dict with the expected keys
    and finite numeric metrics for every test query."""
    from bench.run_real_eval import run

    result = run(corpus_path=CORPUS_PATH, verbose=False)

    assert "summary" in result
    assert "per_query" in result
    assert "stats" in result

    summary = result["summary"]
    for key in ("recall_5", "recall_10", "mrr", "crit_hit"):
        assert key in summary
        mean, stdev = summary[key]
        assert 0.0 <= mean <= 1.0, (key, mean)
        assert stdev >= 0.0, (key, stdev)

    per_query = result["per_query"]
    assert len(per_query) == 5  # the corpus has 5 tests
    for q in per_query:
        for key in ("recall_5", "recall_10", "mrr", "crit_hit"):
            assert 0.0 <= q[key] <= 1.0, q
