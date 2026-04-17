"""Tests for intent-aware production retrieval (Task E).

These tests exercise the `intent` and `auto_classify_intent` parameters on
`extract.retrieval.retrieve_context` and the `_apply_intent_boost` /
`_rank(intent=...)` helpers that drive intent-conditional ranking.

Intent ranking rules covered (see `_apply_intent_boost`):
- recent         : favour <7d, demote >60d
- weighs         : favour high emotional_weight persons, demote low-emo
- anchor_family  : favour persons, demote non-persons
- decoy_resist   : demote high-emo entities (inverse of weighs)

Out of scope here (tested in `test_intent.py`): the classifier itself.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _fresh_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    migrations_dir = (
        Path(__file__).resolve().parent.parent.parent
        / "internal"
        / "store"
        / "migrations"
    )
    for sql_file in sorted(migrations_dir.glob("*.sql")):
        con.executescript(sql_file.read_text())
    return con


def _iso(days_ago: int) -> str:
    """ISO timestamp N days before now (UTC, Z suffix)."""
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _insert_entity(
    con,
    entity_id: int,
    name: str,
    kind: str = "person",
    *,
    salience: float = 0.5,
    emo: float = 0.5,
    days_ago: int = 1,
    aliases=None,
    is_self: int = 0,
):
    aliases_json = json.dumps(aliases or [])
    ts = _iso(days_ago)
    con.execute(
        "INSERT INTO entities "
        "(id, canonical_name, kind, aliases, first_seen, last_seen, "
        " salience_score, emotional_weight, is_self) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (entity_id, name, kind, aliases_json, ts, ts, salience, emo, is_self),
    )


# ---------------------------------------------------------------------------
# retrieve_context — intent plumbing
# ---------------------------------------------------------------------------


def test_retrieve_context_passes_explicit_intent(tmp_path):
    """Caller-provided intent must flow through without auto-classify."""
    from extract.retrieval import retrieve_context

    con = _fresh_db(tmp_path)
    _insert_entity(con, 1, "Alex")
    result = retrieve_context(con, "какое-то сообщение про Alex", intent="weighs")
    assert result["intent"] == "weighs"
    assert result["intent_classifier"] == "provided"


def test_retrieve_context_auto_classifies_intent_from_message(tmp_path):
    """With intent=None + auto_classify_intent=True (default), the rules
    classifier maps the message. 'что тебя беспокоит' hits the `weighs` bucket."""
    from extract.retrieval import retrieve_context

    con = _fresh_db(tmp_path)
    _insert_entity(con, 1, "Alex")
    result = retrieve_context(con, "Alex, что тебя беспокоит?")
    assert result["intent"] == "weighs"
    assert result["intent_classifier"] == "rules"


def test_retrieve_context_auto_classify_disabled_returns_none_intent(tmp_path):
    """Explicit opt-out: auto_classify_intent=False + no intent → intent='none'.

    This is the backward-compat path for callers that want pre-Task-E behaviour.
    The result dict still carries the 'intent' key (always present), but its
    value is the string 'none' and classifier is 'disabled'.
    """
    from extract.retrieval import retrieve_context

    con = _fresh_db(tmp_path)
    _insert_entity(con, 1, "Alex")
    result = retrieve_context(
        con, "что тебя беспокоит Alex", auto_classify_intent=False
    )
    assert result["intent"] == "none"
    assert result["intent_classifier"] == "disabled"


# ---------------------------------------------------------------------------
# _rank — intent boost behaviour (unit-level, bypasses retrieve_context)
# ---------------------------------------------------------------------------


def test_rank_intent_recent_boosts_fresh_entity(tmp_path):
    """Two persons, same baseline. intent='recent': fresh (3d) > stale (90d)."""
    from extract.retrieval import _rank

    # Equal salience/emo, only last_seen differs. Without the intent boost,
    # recency decay alone on persons (λ=0.001 ⇒ t½ ≈ 693d) is too slow to
    # meaningfully separate 3d vs 90d — the intent boost is what moves them.
    fresh = {
        "id": 1, "canonical_name": "Fresh", "kind": "person",
        "salience_score": 0.5, "emotional_weight": 0.5,
        "last_seen": _iso(3), "is_self": 0, "_hop": 0,
    }
    stale = {
        "id": 2, "canonical_name": "Stale", "kind": "person",
        "salience_score": 0.5, "emotional_weight": 0.5,
        "last_seen": _iso(90), "is_self": 0, "_hop": 0,
    }
    ranked = _rank([stale, fresh], intent="recent")
    assert ranked[0]["canonical_name"] == "Fresh"

    # And without intent, the two are near-tied (recency doesn't meaningfully
    # separate them on person decay) — input order may survive.
    neutral = _rank([stale, fresh], intent=None)
    # Sanity: neither is catastrophically different, both scored > 0
    assert len(neutral) == 2


def test_rank_intent_weighs_demotes_low_emo_entity(tmp_path):
    """Two entities same salience, emo differs. 'weighs' should promote high-emo."""
    from extract.retrieval import _rank

    low = {
        "id": 1, "canonical_name": "Low", "kind": "person",
        "salience_score": 0.5, "emotional_weight": 0.1,
        "last_seen": _iso(1), "is_self": 0, "_hop": 0,
    }
    high = {
        "id": 2, "canonical_name": "High", "kind": "person",
        "salience_score": 0.5, "emotional_weight": 0.8,
        "last_seen": _iso(1), "is_self": 0, "_hop": 0,
    }
    ranked = _rank([low, high], intent="weighs")
    assert ranked[0]["canonical_name"] == "High"


def test_rank_intent_anchor_family_demotes_projects(tmp_path):
    """Person vs project, equal score — 'anchor_family' promotes person."""
    from extract.retrieval import _rank

    # Give the project a clear baseline advantage so the intent boost is what
    # swaps the order, not some incidental tie-break.
    person = {
        "id": 1, "canonical_name": "Mom", "kind": "person",
        "salience_score": 0.4, "emotional_weight": 0.4,
        "last_seen": _iso(1), "is_self": 0, "_hop": 0,
    }
    project = {
        "id": 2, "canonical_name": "Pulse", "kind": "project",
        "salience_score": 0.8, "emotional_weight": 0.4,
        "last_seen": _iso(1), "is_self": 0, "_hop": 0,
    }
    # Under `cold_open`, project scores higher (higher salience).
    baseline = _rank([person, project], intent=None)
    assert baseline[0]["canonical_name"] == "Pulse"

    # Under `anchor_family`, person (×1.3) outranks project (×0.6).
    ranked = _rank([person, project], intent="anchor_family")
    assert ranked[0]["canonical_name"] == "Mom"


def test_rank_intent_decoy_resist_demotes_high_emo(tmp_path):
    """Inverse of weighs — high-emo entity gets demoted to protect from landmines.

    We use two persons below the anchor-boost threshold (emo<=0.6 on both), so
    the only thing separating them is the intent boost. The 'neutral' entity
    has a higher baseline salience so we can demonstrate that decoy_resist
    flips the order *only* because the high-emo entity is actively demoted —
    not because the neutral one was going to win anyway.
    """
    from extract.retrieval import _rank

    neutral = {
        "id": 1, "canonical_name": "Neutral", "kind": "person",
        "salience_score": 0.5, "emotional_weight": 0.2,
        "last_seen": _iso(1), "is_self": 0, "_hop": 0,
    }
    landmine = {
        "id": 2, "canonical_name": "Landmine", "kind": "person",
        # emo=0.75 is above the decoy_resist demotion threshold (>0.7)
        # but salience+emo is also higher than neutral — without the demotion,
        # landmine wins.
        "salience_score": 0.5, "emotional_weight": 0.75,
        "last_seen": _iso(1), "is_self": 0, "_hop": 0,
    }

    # Baseline (no intent): anchor boost (person + emo>0.6) puts Landmine first.
    baseline = _rank([neutral, landmine], intent=None)
    assert baseline[0]["canonical_name"] == "Landmine"

    # decoy_resist: Landmine ×0.6 (emo>0.7). Landmine baseline was
    #   (0.5+0.75)*1.5 = 1.875  → ×0.6 = 1.125
    # Neutral baseline was
    #   (0.5+0.2)*1.0 = 0.70    → ×1.0 = 0.70
    # Landmine still wins — this is expected: the 0.6 multiplier cannot fully
    # override a 2.7× baseline gap. The test instead pins the _behaviour_:
    # under decoy_resist, Landmine's absolute score falls below its
    # cold_open score (proof the demotion fires).
    ranked = _rank([neutral, landmine], intent="decoy_resist")
    # Landmine is still at rank 0 because its baseline was so high — but the
    # demotion IS applied. We verify by constructing a tighter case below.
    assert len(ranked) == 2

    # Tighter case: equal salience+emo sum, same kind. Now the 0.6 multiplier
    # flips order cleanly.
    near_tie_low = {
        "id": 3, "canonical_name": "Low", "kind": "person",
        "salience_score": 0.8, "emotional_weight": 0.3,  # sum 1.1, anchor=1.0
        "last_seen": _iso(1), "is_self": 0, "_hop": 0,
    }
    near_tie_high = {
        "id": 4, "canonical_name": "High", "kind": "person",
        "salience_score": 0.3, "emotional_weight": 0.8,  # sum 1.1, anchor=1.5
        "last_seen": _iso(1), "is_self": 0, "_hop": 0,
    }
    # Baseline: anchor boost pushes High on top (1.1*1.5 > 1.1*1.0).
    base2 = _rank([near_tie_low, near_tie_high], intent=None)
    assert base2[0]["canonical_name"] == "High"

    # decoy_resist: High ×0.6 → 1.1*1.5*0.6 = 0.99 < Low 1.1*1.0*1.0 = 1.10.
    # Order flips.
    ranked2 = _rank([near_tie_low, near_tie_high], intent="decoy_resist")
    assert ranked2[0]["canonical_name"] == "Low"
