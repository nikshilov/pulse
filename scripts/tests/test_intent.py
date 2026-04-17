"""Tests for `extract.intent.classify_intent_rules` and `classify_intent_llm`.

Covers the 6 intent labels across English and Russian, edge cases
(empty / whitespace), ordering guarantees (decoy_resist > anchor_family,
weighs > recent), and — crucially — the 5 actual queries in the
empathic-memory-corpus that the LLM-judge bench uses.

The LLM classifier tests are mock-only — no live Anthropic calls. We
assert happy-path tool-use, missing-key error, no-tool-call error, and
the defence-in-depth guard against enum-violating inputs.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

_HERE = Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import pytest  # noqa: E402

from extract.intent import (  # noqa: E402
    classify_intent_llm,
    classify_intent_rules,
)


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


# ---------------------------------------------------------------------------
# LLM classifier — mock-only (no live API calls)
# ---------------------------------------------------------------------------

def _mock_tool_use_block(intent: str, reason: str = "test"):
    """Build a mock tool_use content block matching anthropic SDK shape."""
    return SimpleNamespace(
        type="tool_use",
        name="classify_query_intent",
        input={"intent": intent, "reason": reason},
    )


def _mock_text_block(text: str):
    return SimpleNamespace(type="text", text=text)


def _mock_client_returning(content_blocks):
    """Build a mock Anthropic client whose messages.create returns the given blocks."""
    client = MagicMock()
    client.messages.create.return_value = SimpleNamespace(content=content_blocks)
    return client


def test_classify_intent_llm_tool_use_happy_path():
    """Mock tool_use returns 'recent' for a recency query."""
    client = _mock_client_returning([
        _mock_tool_use_block("recent", reason="asks about 'these days'"),
    ])
    result = classify_intent_llm("How's Alex these days?", client=client)
    assert result == "recent"
    # Ensure the SDK was called with the expected tool-choice shape.
    args, kwargs = client.messages.create.call_args
    assert kwargs["tool_choice"] == {
        "type": "tool", "name": "classify_query_intent",
    }
    assert kwargs["tools"][0]["name"] == "classify_query_intent"


def test_classify_intent_llm_no_key_raises(monkeypatch):
    """No client, no ANTHROPIC_API_KEY in env → RuntimeError."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        classify_intent_llm("anything", client=None)


def test_classify_intent_llm_no_tool_call_raises():
    """Model returned text only (no tool_use block) → RuntimeError."""
    client = _mock_client_returning([
        _mock_text_block("I think this is a recent query."),
    ])
    with pytest.raises(RuntimeError, match="did not call the classifier tool"):
        classify_intent_llm("anything", client=client)


def test_classify_intent_llm_invalid_intent_raises():
    """Tool-use with a value outside the enum → RuntimeError (defence-in-depth).

    Tool-use schema enforcement should make this impossible in production,
    but if the SDK ever relaxes that contract, we fail loud rather than
    silently accepting a hallucinated intent.
    """
    client = _mock_client_returning([
        _mock_tool_use_block("urgent", reason="not a real intent"),
    ])
    with pytest.raises(RuntimeError, match="invalid intent"):
        classify_intent_llm("anything", client=client)


def test_classify_intent_llm_all_valid_enum_values():
    """Sanity — each of the 6 valid intents round-trips through the mock."""
    for intent in (
        "recent", "weighs", "anchor_family",
        "opener", "decoy_resist", "cold_open",
    ):
        client = _mock_client_returning([_mock_tool_use_block(intent)])
        assert classify_intent_llm("q", client=client) == intent
