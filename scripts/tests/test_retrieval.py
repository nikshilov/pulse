"""Tests for keyword-based graph retrieval."""

import json
import sqlite3
from pathlib import Path

import pytest


def _fresh_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    migrations_dir = Path(__file__).resolve().parent.parent.parent / "internal" / "store" / "migrations"
    for sql_file in sorted(migrations_dir.glob("*.sql")):
        con.executescript(sql_file.read_text())
    return con


def _seed_graph(con):
    """Seed a small graph: Anna (person), Nik (person), Pulse (project), with relations and facts."""
    now = "2026-04-16T00:00:00Z"
    con.execute("INSERT INTO entities (id, canonical_name, kind, aliases, first_seen, last_seen, salience_score) VALUES (1,'Anna','person',?,?,?,0.9)", (json.dumps(["Аня", "Анна"]), now, now))
    con.execute("INSERT INTO entities (id, canonical_name, kind, aliases, first_seen, last_seen, salience_score) VALUES (2,'Nik','person',?,?,?,1.0)", (json.dumps(["Никита"]), now, now))
    con.execute("INSERT INTO entities (id, canonical_name, kind, aliases, first_seen, last_seen, salience_score) VALUES (3,'Pulse','project',?,?,?,0.8)", (json.dumps(["pulse-engine"]), now, now))
    con.execute("INSERT INTO relations (from_entity_id, to_entity_id, kind, strength, context, first_seen, last_seen) VALUES (1,2,'spouse',1.0,'married since 2020',?,?)", (now, now))
    con.execute("INSERT INTO relations (from_entity_id, to_entity_id, kind, strength, first_seen, last_seen) VALUES (2,3,'creator',1.0,?,?)", (now, now))
    con.execute("INSERT INTO facts (entity_id, text, confidence, created_at) VALUES (1,'Loves cats',0.9,?)", (now,))
    con.execute("INSERT INTO facts (entity_id, text, confidence, created_at) VALUES (3,'Written in Go and Python',0.95,?)", (now,))


def test_retrieve_by_canonical_name(tmp_path):
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    _seed_graph(con)
    result = retrieve_context(con, "Давай обсудим Anna")
    assert result["total_matched"] >= 1
    names = [e["canonical_name"] for e in result["matched_entities"]]
    assert "Anna" in names


def test_retrieve_by_alias(tmp_path):
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    _seed_graph(con)
    result = retrieve_context(con, "Аня сегодня устала")
    names = [e["canonical_name"] for e in result["matched_entities"]]
    assert "Anna" in names


def test_retrieve_includes_relations(tmp_path):
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    _seed_graph(con)
    result = retrieve_context(con, "Anna")
    anna = [e for e in result["matched_entities"] if e["canonical_name"] == "Anna"][0]
    rel_kinds = [r["kind"] for r in anna["relations"]]
    assert "spouse" in rel_kinds


def test_retrieve_includes_facts(tmp_path):
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    _seed_graph(con)
    result = retrieve_context(con, "Anna")
    anna = [e for e in result["matched_entities"] if e["canonical_name"] == "Anna"][0]
    fact_texts = [f["text"] for f in anna["facts"]]
    assert "Loves cats" in fact_texts


def test_retrieve_respects_top_k(tmp_path):
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    _seed_graph(con)
    result = retrieve_context(con, "Anna Nik Pulse", top_k=2)
    assert len(result["matched_entities"]) <= 2


def test_retrieve_no_match_returns_empty(tmp_path):
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    _seed_graph(con)
    result = retrieve_context(con, "XYZNONEXISTENT")
    assert result["total_matched"] == 0
    assert result["matched_entities"] == []


def test_retrieve_method_is_keyword(tmp_path):
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    _seed_graph(con)
    result = retrieve_context(con, "Anna")
    assert result["retrieval_method"] == "keyword"


def test_retrieve_2hop_indirect_relation(tmp_path):
    """Anna→Nik→Pulse: querying 'Anna' with depth=2 should find Pulse via Nik."""
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    _seed_graph(con)
    result = retrieve_context(con, "Anna", depth=2)
    names = [e["canonical_name"] for e in result["matched_entities"]]
    assert "Anna" in names
    assert "Pulse" in names  # found via Anna→Nik→Pulse (2 hops)


def test_hop_penalty_ranking(tmp_path):
    """Direct match should rank above 2-hop match."""
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    _seed_graph(con)
    result = retrieve_context(con, "Anna", depth=2)
    entities = result["matched_entities"]
    anna_idx = next(i for i, e in enumerate(entities) if e["canonical_name"] == "Anna")
    pulse_idx = next(i for i, e in enumerate(entities) if e["canonical_name"] == "Pulse")
    assert anna_idx < pulse_idx  # Anna (direct) ranks above Pulse (2 hops away)


def test_depth_0_returns_only_matched(tmp_path):
    """depth=0 returns matched entity without expanding relations."""
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    _seed_graph(con)
    result = retrieve_context(con, "Anna", depth=0)
    names = [e["canonical_name"] for e in result["matched_entities"]]
    assert "Anna" in names
    assert "Nik" not in names  # no expansion


def test_retrieve_survives_corrupted_aliases_json(tmp_path):
    """Entity with invalid JSON in aliases column should not crash retrieval."""
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    now = "2026-04-16T00:00:00Z"
    con.execute(
        "INSERT INTO entities (id, canonical_name, kind, aliases, first_seen, last_seen, salience_score) "
        "VALUES (1,'BadEntity','person','NOT VALID JSON',?,?,0.5)", (now, now)
    )
    con.execute(
        "INSERT INTO entities (id, canonical_name, kind, aliases, first_seen, last_seen, salience_score) "
        "VALUES (2,'GoodEntity','person',?,?,?,0.8)", (json.dumps(["good"]), now, now)
    )
    result = retrieve_context(con, "GoodEntity")
    names = [e["canonical_name"] for e in result["matched_entities"]]
    assert "GoodEntity" in names


def test_retrieve_handles_zero_salience(tmp_path):
    """Entity with salience_score=0 should not crash ranking."""
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    now = "2026-04-16T00:00:00Z"
    con.execute(
        "INSERT INTO entities (id, canonical_name, kind, aliases, first_seen, last_seen, salience_score) "
        "VALUES (1,'ZeroScore','person',?,?,?,0.0)", (json.dumps([]), now, now)
    )
    result = retrieve_context(con, "ZeroScore")
    assert result["total_matched"] == 1
    assert result["matched_entities"][0]["salience_score"] == 0.0


# ---------------------------------------------------------------------------
# Garden-style ranking: emotional_weight + anchor boost + kind-aware decay
# ---------------------------------------------------------------------------

def test_rank_emotional_weight_beats_equal_salience(tmp_path):
    """Two persons with equal salience but different emotional_weight: high-emo ranks first."""
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    now = "2026-04-16T00:00:00Z"
    # Same name → message matches both; same salience → only emo differentiates
    con.execute(
        "INSERT INTO entities (id, canonical_name, kind, aliases, first_seen, last_seen, salience_score, emotional_weight) "
        "VALUES (1,'Shared','person',?,?,?,0.5,0.9)", (json.dumps(["Shared"]), now, now)
    )
    con.execute(
        "INSERT INTO entities (id, canonical_name, kind, aliases, first_seen, last_seen, salience_score, emotional_weight) "
        "VALUES (2,'Other','person',?,?,?,0.5,0.0)", (json.dumps(["Shared"]), now, now)
    )
    result = retrieve_context(con, "Shared")
    # Both matched by alias "Shared"; id=1 has emo=0.9 → higher score
    assert result["matched_entities"][0]["id"] == 1


def test_rank_emotional_weight_additive_not_multiplicative(tmp_path):
    """Low-salience high-emo should beat high-salience zero-emo (additive model).

    Without additive formula, a salience=0.9/emo=0 entity wins. With (salience+emo),
    salience=0.3/emo=0.9 = 1.2 total, vs salience=0.9/emo=0 = 0.9.
    """
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    now = "2026-04-16T00:00:00Z"
    con.execute(
        "INSERT INTO entities (id, canonical_name, kind, aliases, first_seen, last_seen, salience_score, emotional_weight) "
        "VALUES (1,'Wound','concept',?,?,?,0.3,0.9)", (json.dumps(["Keyword"]), now, now)
    )
    con.execute(
        "INSERT INTO entities (id, canonical_name, kind, aliases, first_seen, last_seen, salience_score, emotional_weight) "
        "VALUES (2,'Trivia','concept',?,?,?,0.9,0.0)", (json.dumps(["Keyword"]), now, now)
    )
    result = retrieve_context(con, "Keyword")
    assert result["matched_entities"][0]["canonical_name"] == "Wound"


def test_rank_anchor_boost_for_emotional_persons(tmp_path):
    """Person with emo>0.6 gets 1.5× anchor boost over a project with same totals."""
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    now = "2026-04-16T00:00:00Z"
    # Person: (0.2+0.7)*recency*1.5 = 1.35*recency
    # Project: (0.2+0.7)*recency*1.0 = 0.9*recency
    con.execute(
        "INSERT INTO entities (id, canonical_name, kind, aliases, first_seen, last_seen, salience_score, emotional_weight) "
        "VALUES (1,'PersonA','person',?,?,?,0.2,0.7)", (json.dumps(["target"]), now, now)
    )
    con.execute(
        "INSERT INTO entities (id, canonical_name, kind, aliases, first_seen, last_seen, salience_score, emotional_weight) "
        "VALUES (2,'ProjectA','project',?,?,?,0.2,0.7)", (json.dumps(["target"]), now, now)
    )
    result = retrieve_context(con, "target")
    assert result["matched_entities"][0]["canonical_name"] == "PersonA"


def test_bfs_skips_do_not_probe_neighbor(tmp_path):
    """A→B→C chain where B has do_not_probe=1: seeding by A at depth=2 must NOT return B or C.

    B is the blocker — BFS refuses to yield B as a neighbor at all, so C (reachable
    only through B) never gets discovered. This is the structural safety gate for
    graph expansion: entities behind the opt-out are invisible to retrieval.
    """
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    now = "2026-04-16T00:00:00Z"
    con.execute(
        "INSERT INTO entities (id, canonical_name, kind, aliases, first_seen, last_seen, salience_score, do_not_probe) "
        "VALUES (1, 'Alpha',   'person', ?, ?, ?, 0.9, 0)",
        (json.dumps([]), now, now),
    )
    con.execute(
        "INSERT INTO entities (id, canonical_name, kind, aliases, first_seen, last_seen, salience_score, do_not_probe) "
        "VALUES (2, 'BlockedBravo', 'person', ?, ?, ?, 0.9, 1)",
        (json.dumps([]), now, now),
    )
    con.execute(
        "INSERT INTO entities (id, canonical_name, kind, aliases, first_seen, last_seen, salience_score, do_not_probe) "
        "VALUES (3, 'Charlie', 'person', ?, ?, ?, 0.9, 0)",
        (json.dumps([]), now, now),
    )
    # A → B (strong), B → C (strong)
    con.execute(
        "INSERT INTO relations (from_entity_id, to_entity_id, kind, strength, first_seen, last_seen) "
        "VALUES (1, 2, 'knows', 1.0, ?, ?)", (now, now),
    )
    con.execute(
        "INSERT INTO relations (from_entity_id, to_entity_id, kind, strength, first_seen, last_seen) "
        "VALUES (2, 3, 'knows', 1.0, ?, ?)", (now, now),
    )

    result = retrieve_context(con, "Alpha", depth=2)
    names = [e["canonical_name"] for e in result["matched_entities"]]
    assert "Alpha" in names
    # B is the neighbor that should be suppressed — neither returned itself …
    assert "BlockedBravo" not in names
    # … nor used as a bridge to reach C.
    assert "Charlie" not in names


def test_rank_skips_do_not_probe_entity_in_results(tmp_path):
    """A direct-name match that is flagged `do_not_probe=1` must not appear in the
    ranked results at all. Judge 2/6 observation: the BFS gate alone is half-done;
    a trauma entity directly named in the message was still landing in top-k as a
    seed match. Seed-level gating closes that hole.
    """
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    now = "2026-04-16T00:00:00Z"
    # Optout entity has a strong salience and is directly aliased to the query term.
    con.execute(
        "INSERT INTO entities (id, canonical_name, kind, aliases, first_seen, last_seen, salience_score, emotional_weight, do_not_probe) "
        "VALUES (1, 'Kristina', 'person', ?, ?, ?, 0.9, 0.95, 1)",
        (json.dumps(["Кристина"]), now, now),
    )
    # A safe companion match on the same token so total_matched != 0 baseline stays meaningful.
    con.execute(
        "INSERT INTO entities (id, canonical_name, kind, aliases, first_seen, last_seen, salience_score) "
        "VALUES (2, 'SafeOne', 'person', ?, ?, ?, 0.4)",
        (json.dumps(["Кристина"]), now, now),
    )
    result = retrieve_context(con, "Кристина")
    names = [e["canonical_name"] for e in result["matched_entities"]]
    assert "Kristina" not in names
    assert "SafeOne" in names


def test_rank_self_entity_no_anchor_boost(tmp_path):
    """Two equal persons (same salience, self has higher emo): non-self still wins
    because self-entity has anchor stripped to 1.0 while non-self gets 1.5 boost.
    Judge 1/6 observation: bench had Nik top-1 in 11/15 queries regardless of
    subject because anchor×1.5 on the self-entity dominated every rank contest.
    """
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    now = "2026-04-16T00:00:00Z"
    # Self: salience=0.8, emo=0.9 → (0.8+0.9)*recency*1.0 = 1.7
    # Other: salience=0.8, emo=0.7 → (0.8+0.7)*recency*1.5 = 2.25
    con.execute(
        "INSERT INTO entities (id, canonical_name, kind, aliases, first_seen, last_seen, salience_score, emotional_weight, is_self) "
        "VALUES (1, 'Nik', 'person', ?, ?, ?, 0.8, 0.9, 1)",
        (json.dumps(["target"]), now, now),
    )
    con.execute(
        "INSERT INTO entities (id, canonical_name, kind, aliases, first_seen, last_seen, salience_score, emotional_weight, is_self) "
        "VALUES (2, 'Anna', 'person', ?, ?, ?, 0.8, 0.7, 0)",
        (json.dumps(["target"]), now, now),
    )
    result = retrieve_context(con, "target")
    # Anna ranks first: anchor boost applies to her, not to the self-entity Nik.
    assert result["matched_entities"][0]["canonical_name"] == "Anna"


def test_self_entity_still_appears_when_directly_matched(tmp_path):
    """Stripping the anchor boost must NOT exclude the self-entity from results —
    Nik can still appear when directly named, just without anchor pressure.
    """
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    now = "2026-04-16T00:00:00Z"
    con.execute(
        "INSERT INTO entities (id, canonical_name, kind, aliases, first_seen, last_seen, salience_score, emotional_weight, is_self) "
        "VALUES (1, 'Nik', 'person', ?, ?, ?, 1.0, 0.9, 1)",
        (json.dumps(["Никита"]), now, now),
    )
    result = retrieve_context(con, "Никита")
    names = [e["canonical_name"] for e in result["matched_entities"]]
    assert "Nik" in names


def test_rank_kind_aware_decay_concept_fades_faster(tmp_path):
    """Concept (λ=0.01) 100 days stale loses to person (λ=0.001) 100 days stale."""
    from extract.retrieval import retrieve_context
    con = _fresh_db(tmp_path)
    now = "2026-04-16T00:00:00Z"
    stale = "2026-01-06T00:00:00Z"  # 100 days earlier
    # Equal salience, equal emo, equal hop — only kind-decay separates them
    con.execute(
        "INSERT INTO entities (id, canonical_name, kind, aliases, first_seen, last_seen, salience_score, emotional_weight) "
        "VALUES (1,'StaleConcept','concept',?,?,?,0.5,0.3)", (json.dumps(["target"]), stale, stale)
    )
    con.execute(
        "INSERT INTO entities (id, canonical_name, kind, aliases, first_seen, last_seen, salience_score, emotional_weight) "
        "VALUES (2,'StalePerson','person',?,?,?,0.5,0.3)", (json.dumps(["target"]), stale, stale)
    )
    # person λ=0.001 × 100 = 0.1 → e^-0.1 ≈ 0.905
    # concept λ=0.01 × 100 = 1.0 → e^-1.0 ≈ 0.368
    result = retrieve_context(con, "target")
    assert result["matched_entities"][0]["canonical_name"] == "StalePerson"
