"""Tests for `rank_memories_by_intent` in scripts/bench/run_llm_judge.py.

These tests pin the ranking contract per intent so downstream bench
numbers don't silently drift. Each test feeds a small hand-crafted pool
of memory dicts and asserts the expected ordering.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from bench.run_llm_judge import rank_memories_by_intent  # noqa: E402


def _mem(id, text="", sentiment=0, user_flag=0, days_ago=30):
    return {
        "id": id,
        "text": text,
        "sentiment": sentiment,
        "user_flag": user_flag,
        "days_ago": days_ago,
    }


# ---------------------------------------------------------------------------
# recent
# ---------------------------------------------------------------------------

def test_rank_recent_puts_fresh_emotional_first():
    """Fresh events win, but flag/sentiment weight beats pure freshness.

    2d-old mundane (adj=2) vs 14d-old flagged+|2| (adj=14-14-30=-30) →
    the engagement (with flag + emotion) wins despite being older.
    Old grief (adj=365-14-30=321) stays last.
    """
    pool = [
        _mem(1, "old grief anchor", sentiment=-2, user_flag=1, days_ago=365),
        _mem(2, "engagement", sentiment=2, user_flag=1, days_ago=14),
        _mem(3, "mundane gym", sentiment=0, user_flag=0, days_ago=2),
    ]
    ranked = rank_memories_by_intent(pool, "recent")
    assert ranked[0]["id"] == 2       # flagged + emotional beats mundane
    assert ranked[1]["id"] == 3       # mundane but fresh
    assert ranked[-1]["id"] == 1      # year-old, deprioritized even with flag


def test_rank_recent_neutral_mundane_loses_to_slightly_older_emotional():
    """A 4d gym event with sent=0 flag=0 (adj=4) loses to a 14d engagement
    sent=+2 flag=1 (adj=-30). This is the T4 failure we targeted."""
    pool = [
        _mem(17, "gym 4 days", sentiment=0, user_flag=0, days_ago=4),
        _mem(2, "engagement 14 days", sentiment=2, user_flag=1, days_ago=14),
    ]
    ranked = rank_memories_by_intent(pool, "recent")
    assert ranked[0]["id"] == 2


def test_rank_recent_same_adjustment_tiebreaks_to_fresher():
    pool = [
        _mem(1, sentiment=-1, user_flag=0, days_ago=5),
        _mem(2, sentiment=-1, user_flag=0, days_ago=3),
    ]
    # Same sentiment, same flag → adjusted = days - 7. Fresher (3) wins.
    ranked = rank_memories_by_intent(pool, "recent")
    assert ranked[0]["id"] == 2


# ---------------------------------------------------------------------------
# weighs
# ---------------------------------------------------------------------------

def test_rank_weighs_filters_to_negative_sentiment():
    pool = [
        _mem(1, "engagement (positive)", sentiment=2, user_flag=1, days_ago=14),
        _mem(2, "grief anchor", sentiment=-2, user_flag=1, days_ago=365),
        _mem(3, "private health", sentiment=-1, user_flag=1, days_ago=120),
        _mem(4, "dad hospital", sentiment=-2, user_flag=1, days_ago=30),
    ]
    ranked = rank_memories_by_intent(pool, "weighs")
    # Negatives first, by abs(sentiment) DESC, days_ago ASC tiebreak.
    # Ties at |2|: id=4 (days=30) before id=2 (days=365). Then id=3 (|1|).
    # Then positives padded at the end.
    assert [m["id"] for m in ranked[:3]] == [4, 2, 3]
    # Positive NEVER in top-3 when we have ≥3 negatives.
    assert 1 not in [m["id"] for m in ranked[:3]]


def test_rank_weighs_pads_when_insufficient_negatives():
    pool = [
        _mem(1, "engagement", sentiment=2, user_flag=1, days_ago=14),
        _mem(2, "grief", sentiment=-2, user_flag=1, days_ago=365),
        _mem(3, "neutral", sentiment=0, user_flag=0, days_ago=30),
    ]
    ranked = rank_memories_by_intent(pool, "weighs")
    # Only one negative (id=2) — it comes first, rest padded by
    # abs(sentiment) DESC, which puts id=1 (|2|) before id=3 (|0|).
    assert ranked[0]["id"] == 2
    assert set(m["id"] for m in ranked) == {1, 2, 3}
    padding_ids = [m["id"] for m in ranked[1:]]
    assert padding_ids.index(1) < padding_ids.index(3)


# ---------------------------------------------------------------------------
# anchor_family
# ---------------------------------------------------------------------------

def test_rank_anchor_family_filters_to_family_texts():
    pool = [
        _mem(1, "Alex's mom Sarah died of cancer — anchor",
             sentiment=-2, user_flag=1, days_ago=365),
        _mem(2, "apartment move — no family content",
             sentiment=0, user_flag=0, days_ago=60),
        _mem(3, "saw his brother Ethan for beers",
             sentiment=1, user_flag=0, days_ago=20),
        _mem(4, "apple cake with mom (photo)",
             sentiment=-1, user_flag=0, days_ago=100),
    ]
    ranked = rank_memories_by_intent(pool, "anchor_family")
    top3 = [m["id"] for m in ranked[:3]]
    # All three family entries should surface; non-family moves to the back.
    assert set(top3) == {1, 3, 4}
    # Anchor (user_flag=1) is slot 1.
    assert ranked[0]["id"] == 1
    # Non-family is deprioritized.
    assert ranked[-1]["id"] == 2


def test_rank_anchor_family_pads_with_flagged_globals():
    pool = [
        _mem(1, "random work event", sentiment=0, user_flag=1, days_ago=30),
        _mem(2, "general neutral event", sentiment=0, user_flag=0, days_ago=10),
    ]
    # No family mentions — padding path kicks in.
    ranked = rank_memories_by_intent(pool, "anchor_family")
    # Family set empty → flagged pad first, then the rest.
    assert ranked[0]["id"] == 1
    assert ranked[1]["id"] == 2


# ---------------------------------------------------------------------------
# opener
# ---------------------------------------------------------------------------

def test_rank_opener_prefers_flagged_and_weighty():
    pool = [
        _mem(1, sentiment=0, user_flag=0, days_ago=5),
        _mem(2, sentiment=-2, user_flag=1, days_ago=365),
        _mem(3, sentiment=2, user_flag=1, days_ago=14),
        _mem(4, sentiment=-2, user_flag=0, days_ago=30),
    ]
    ranked = rank_memories_by_intent(pool, "opener")
    # Flagged pair (2, 3) first — both |2|, order stable by input. Then
    # unflagged with higher |sentiment| (4), then neutral (1).
    top2 = [m["id"] for m in ranked[:2]]
    assert set(top2) == {2, 3}
    assert ranked[2]["id"] == 4
    assert ranked[3]["id"] == 1


# ---------------------------------------------------------------------------
# decoy_resist
# ---------------------------------------------------------------------------

def test_rank_decoy_resist_demotes_grief_anchor():
    pool = [
        _mem(1, "engagement to Maya", sentiment=2, user_flag=1, days_ago=14),
        _mem(2, "mom grief anchor", sentiment=-2, user_flag=1, days_ago=365),
        _mem(3, "apartment move", sentiment=0, user_flag=0, days_ago=60),
        _mem(4, "sister photo moment", sentiment=1, user_flag=0, days_ago=50),
    ]
    ranked = rank_memories_by_intent(pool, "decoy_resist")
    # Positives first, by sentiment DESC — id=1 (|+2|) > id=4 (|+1|).
    assert ranked[0]["id"] == 1
    # Grief anchor (id=2) MUST NOT be top-1.
    assert ranked[0]["id"] != 2
    # But it IS promoted into slot 2 as a safety anchor.
    assert ranked[1]["id"] == 2


def test_rank_decoy_resist_no_anchor_case_pure_positive_sort():
    pool = [
        _mem(1, "joy big", sentiment=2, user_flag=0, days_ago=14),
        _mem(2, "joy small", sentiment=1, user_flag=0, days_ago=30),
        _mem(3, "neutral", sentiment=0, user_flag=0, days_ago=5),
    ]
    ranked = rank_memories_by_intent(pool, "decoy_resist")
    assert [m["id"] for m in ranked] == [1, 2, 3]


# ---------------------------------------------------------------------------
# cold_open — baseline preservation
# ---------------------------------------------------------------------------

def test_rank_cold_open_preserves_baseline_behavior():
    """cold_open must reproduce the old (flag-first, |sentiment| tiebreak) order."""
    pool = [
        _mem(1, "mundane", sentiment=0, user_flag=0, days_ago=5),
        _mem(2, "grief anchor", sentiment=-2, user_flag=1, days_ago=365),
        _mem(3, "engagement", sentiment=2, user_flag=1, days_ago=14),
        _mem(4, "strong neg", sentiment=-2, user_flag=0, days_ago=30),
    ]
    ranked = rank_memories_by_intent(pool, "cold_open")
    # Flagged pair first, both |2|; then unflagged by |sentiment|.
    assert {m["id"] for m in ranked[:2]} == {2, 3}
    assert ranked[2]["id"] == 4
    assert ranked[3]["id"] == 1


def test_rank_empty_pool_returns_empty():
    assert rank_memories_by_intent([], "cold_open") == []
    assert rank_memories_by_intent([], "weighs") == []
    assert rank_memories_by_intent([], "recent") == []


def test_rank_unknown_intent_falls_back_to_cold_open():
    pool = [
        _mem(1, sentiment=0, user_flag=0),
        _mem(2, sentiment=-1, user_flag=1),
    ]
    # Nonexistent intent string should not crash — it uses cold_open.
    ranked = rank_memories_by_intent(pool, "NOT_A_REAL_INTENT")
    assert ranked[0]["id"] == 2
