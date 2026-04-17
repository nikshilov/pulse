"""Tests for `extract.intent.classify_intent_rules`.

Covers the 6 intent labels across English and Russian, edge cases
(empty / whitespace), ordering guarantees (decoy_resist > anchor_family,
weighs > recent), and — crucially — the 5 actual queries in the
empathic-memory-corpus that the LLM-judge bench uses.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import pytest  # noqa: E402

from extract.intent import classify_intent_rules  # noqa: E402


# ---------------------------------------------------------------------------
# Per-intent smoke tests (EN + RU)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("q", [
    "What has happened in Alex's life recently?",
    "What's been going on lately for Alex?",
    "Tell me what's new with Alex these days.",
    "Что нового у Алекса?",
    "Что с ним в последнее время?",
    "Что было за последние дни?",
])
def test_recent_intent(q):
    assert classify_intent_rules(q) == "recent"


@pytest.mark.parametrize("q", [
    "What is currently weighing on Alex emotionally?",
    "What's bothering him right now?",
    "Is Alex struggling with anything?",
    "Has something been heavy for Alex?",
    "Что тяжело у него сейчас?",
    "Что его тревожит?",
    "Что давит?",
])
def test_weighs_intent(q):
    assert classify_intent_rules(q) == "weighs"


@pytest.mark.parametrize("q", [
    "Tell me about Alex's family.",
    "I want to ask Alex about his family this weekend. What should I know?",
    "What about his mom?",
    "Does he have a brother?",
    "How's his dad?",
    "Расскажи про его семью.",
    "Что с мамой Алекса?",
    "У него есть брат?",
])
def test_anchor_family_intent(q):
    assert classify_intent_rules(q) == "anchor_family"


@pytest.mark.parametrize("q", [
    "How are you today?",
    "how's it going",
    "How have you been?",
    "Как дела?",
    "Как ты?",
    "Как у тебя дела?",
    "Привет!",
])
def test_opener_intent(q):
    assert classify_intent_rules(q) == "opener"


@pytest.mark.parametrize("q", [
    "Tell me something warm about Alex.",
    "Give me something light before we talk about the hard stuff.",
    "Can you cheer me up with something good?",
    "Расскажи что-нибудь тёплое про Алекса.",
    "Дай что-нибудь лёгкое.",
    "Что-нибудь хорошее?",
])
def test_decoy_resist_intent(q):
    assert classify_intent_rules(q) == "decoy_resist"


@pytest.mark.parametrize("q", [
    "What is currently most important for Alex? Bring me into context for the next conversation.",
    "Catch me up on Alex.",
    "What's going on with the project?",
    "Что происходит?",
    "Что важного?",
])
def test_cold_open_intent(q):
    assert classify_intent_rules(q) == "cold_open"


# ---------------------------------------------------------------------------
# Ordering / precedence tests
# ---------------------------------------------------------------------------

def test_decoy_beats_anchor_family():
    """'something warm about his mom' → decoy_resist, not anchor_family.

    The decoy-resist strategy protects against surfacing grief content when
    the query is explicitly asking for lightness; anchor_family would walk
    straight into the wound.
    """
    assert (
        classify_intent_rules(
            "Tell me something warm about Alex's mom."
        )
        == "decoy_resist"
    )


def test_weighs_beats_recent():
    """'been weighing on him lately' → weighs, not recent.

    The weighs strategy is the stronger signal (filter to negatives). The
    recent strategy would surface any fresh event regardless of sentiment.
    """
    assert (
        classify_intent_rules(
            "What's been weighing on Alex lately?"
        )
        == "weighs"
    )


def test_anchor_family_beats_opener():
    """Any family mention overrides a generic opener."""
    assert (
        classify_intent_rules(
            "How are you — tell me about his family."
        )
        == "anchor_family"
    )


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("q", ["", "   ", "\n\t"])
def test_empty_falls_through_to_cold_open(q):
    assert classify_intent_rules(q) == "cold_open"


def test_case_insensitivity():
    assert classify_intent_rules("WHAT'S BEEN HAPPENING LATELY?") == "recent"
    assert classify_intent_rules("FAMILY") == "anchor_family"


# ---------------------------------------------------------------------------
# The 5 corpus test queries used by run_llm_judge.py
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("query, expected", [
    # T1 cold_open_salience
    ("What is currently most important for Alex? "
     "Bring me into context for the next conversation.",
     "cold_open"),
    # T2 anchor_obedience
    ("I want to ask Alex about his family this weekend. "
     "What should I know?",
     "anchor_family"),
    # T3 sentiment_weighted_what_weighs
    ("What is currently weighing on Alex emotionally?",
     "weighs"),
    # T4 recency_aware_state
    ("What has happened in Alex's life recently that I should know about?",
     "recent"),
    # T5 decoy_resistance_grief — the actual query is about mom
    ("Tell me about Alex's mom.",
     "anchor_family"),
])
def test_corpus_queries_classification(query, expected):
    """The actual 5 queries in empathic-memory-corpus.

    T5 is a subtle case: its NAME is decoy_resistance_grief but its query
    is a direct family question ("Tell me about Alex's mom"). Our rule-based
    classifier routes it to anchor_family, which is acceptable — the anchor
    warning is also user_flagged, so the anchor_family filter surfaces it
    first. A future LLM-based classifier could recognize the latent decoy-
    resist semantics, but the rule-based path is sufficient for the bench.
    """
    assert classify_intent_rules(query) == expected
