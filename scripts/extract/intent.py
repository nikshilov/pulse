"""Query-intent classification for intent-aware memory ranking.

The memory-level LLM-judge bench (`scripts/bench/run_llm_judge.py`) showed
that Pulse's uniform `user_flag + |sentiment|` ranking is Garden-incompatible:
for "what's happening lately" it returns a 365-day grief anchor; for
"what's weighing" it returns the engagement (positive). Garden switches
ranking strategy per query intent.

This module provides a fast, rule-based classifier — no LLM calls, no
dependencies — that maps a user query to one of six intent labels. The
labels drive `rank_memories_by_intent` in the bench runner.

Schema (ordered by specificity — check more specific intents first):

- recent        : query asks about freshness
                  ("lately", "recently", "these days",
                   "последн", "недавно", "в последнее время")
- weighs        : query asks what's heavy / hard / bothering
                  ("weighing on", "bothering", "struggling",
                   "что тяжело", "тревожит", "давит")
- anchor_family : query asks about family members / relationships
                  ("family", "mom", "dad", "brother", "sister",
                   "семья", "мама", "папа", "брат", "сестра")
- opener        : conversation opener, general "how are you"
                  ("how are you", "как дела", "как ты")
- decoy_resist  : asks for something light / warm while emotional
                  risk is nearby ("tell me something warm", "happy",
                  "что-нибудь лёгкое", "тепл")
- cold_open     : DEFAULT — generic context-fetch with no specific
                  intent ("what's important", "bring me into context",
                   "что происходит", "что важного")

Rules are intentionally permissive. When in doubt, fall through to
`cold_open` — the baseline formula works well there.

An optional LLM-based classifier (`classify_intent_llm`) is also exposed
as a safety net for queries the rules miss — indirect phrasing, irony,
implicit intent, mixed-language — at ~$0.001/query (Sonnet tool-use).
Rules remain the production default; the LLM backend is opt-in.
"""

from __future__ import annotations

import os
import re
from typing import Literal

Intent = Literal[
    "recent",
    "weighs",
    "anchor_family",
    "opener",
    "decoy_resist",
    "cold_open",
]


# Order matters: more specific patterns first.
# Each entry: (intent, [regex_patterns]). All patterns are re.IGNORECASE.
#
# Note on Russian boundaries: Python's `\b` only fires on ASCII word-boundary
# transitions, so `\bмам\b` does NOT match "мамой". For Cyrillic we use
# stem-based substring matching (no `\b`) or a custom non-letter boundary
# `(?<![А-Яа-яЁё])` / `(?![А-Яа-яЁё])`.

_CYR = r"А-Яа-яЁё"
_LB = rf"(?<![{_CYR}])"   # left Cyrillic-aware boundary
_RB = rf"(?![{_CYR}])"    # right Cyrillic-aware boundary

_DECOY_PATTERNS = [
    r"\bsomething\s+warm\b",
    r"\bsomething\s+happy\b",
    r"\bsomething\s+light\b",
    r"\bsomething\s+good\b",
    r"\bsomething\s+nice\b",
    r"\bon a lighter note\b",
    r"\bcheer\s+(me|him|her)\s+up\b",
    # Russian: "что-нибудь/что-то + тепл/лёгк/хорош/приятн/весел/радостн" stems
    rf"что[-\s]нибудь\s+(тепл|лёгк|легк|хорош|приятн|весел|радостн|свет)",
    rf"что[-\s]то\s+(тепл|лёгк|легк|хорош|приятн|весел|радостн|свет)",
    rf"на\s+лёгк",
    # bare adjective-stem "тёпл/тепл" anywhere (matches тёплое, теплое, тёплую)
    rf"{_LB}(тёпл|тепл)",
    rf"{_LB}лёгк(ое|ую|ая|ий|им|ом)",
]

_RECENT_PATTERNS = [
    r"\blately\b",
    r"\brecently\b",
    r"\brecent\b",
    r"\bthese\s+days\b",
    r"\bpast\s+(few\s+)?(days|weeks|week)\b",
    r"\blast\s+(few\s+)?(days|weeks|week)\b",
    r"\bnew\s+(with|for)\b",           # "what's new with Alex"
    r"\bwhat\s+has\s+happened\b",
    r"\bhas\s+happened\s+.*\brecently\b",
    # Russian — stems, Cyrillic-boundary where needed.
    rf"{_LB}недавно{_RB}",
    rf"{_LB}последн",                  # последний/последнее/последние
    rf"в\s+последнее\s+время",
    rf"за\s+последн",
    rf"{_LB}на\s+днях{_RB}",
    rf"что\s+нового",
]

_WEIGHS_PATTERNS = [
    r"\bweigh(s|ing)?\s+on\b",
    r"\bweighing\b",
    r"\bbothering\b",
    r"\bstruggling\b",
    r"\bhard\s+(for|on)\b",
    r"\bhard\s+time\b",
    r"\bheavy\b",
    r"\bpainful\b",
    r"\bstressed\b",
    r"\banxious\b",
    r"\bworr(ied|ying)\b",
    r"\bupset\b",
    rf"что\s+тяжело",
    rf"{_LB}тяжело{_RB}",
    rf"{_LB}тревожит",
    rf"{_LB}давит{_RB}",
    rf"{_LB}переживает",
    rf"{_LB}беспокоит",
    rf"что\s+не\s+так",
    rf"{_LB}грустит",
]

_ANCHOR_FAMILY_PATTERNS = [
    r"\bfamily\b",
    r"\bfamilies\b",
    r"\bmom\b",
    r"\bmum\b",
    r"\bmother\b",
    r"\bdad\b",
    r"\bfather\b",
    r"\bparents?\b",
    r"\bbrother\b",
    r"\bsister\b",
    r"\bsiblings?\b",
    r"\bson\b",
    r"\bdaughter\b",
    r"\bwife\b",
    r"\bhusband\b",
    r"\bspouse\b",
    r"\bfiancé(e)?\b",
    r"\bfiance(e)?\b",
    r"\bpartner\b",
    # Russian — stem matching with Cyrillic-aware left boundary.
    rf"{_LB}семь(я|и|е|ю|ей|ях|ями)",
    rf"{_LB}мам(а|ы|у|е|ой|ами|ах|ой)?{_RB}",
    rf"{_LB}мать{_RB}",
    rf"{_LB}пап(а|ы|у|е|ой|ами|ах)?{_RB}",
    rf"{_LB}отец{_RB}",
    rf"{_LB}отца{_RB}",
    rf"{_LB}брат(а|у|ом|е|ья|ьев|ьям)?{_RB}",
    rf"{_LB}сестр(а|ы|у|е|ой|ами|ах|ы)?{_RB}",
    rf"{_LB}сын(а|у|ом|е|овья|овей|овьям)?{_RB}",
    rf"{_LB}дочь{_RB}",
    rf"{_LB}дочк",
    rf"{_LB}дочер",
    rf"{_LB}жен(а|ы|у|е|ой|ами|ах)?{_RB}",
    rf"{_LB}муж(а|у|ем|е|ья|ей|ьями)?{_RB}",
    rf"{_LB}родител",
]

_OPENER_PATTERNS = [
    r"^\s*how\s+are\s+you\b",
    r"^\s*how.?s\s+it\s+going\b",
    r"^\s*how\s+have\s+you\s+been\b",
    rf"^\s*как\s+дела",
    rf"^\s*как\s+ты{_RB}",
    rf"^\s*как\s+у\s+тебя",
    rf"^\s*привет",
]


def _match_any(text: str, patterns: list[str]) -> bool:
    for p in patterns:
        if re.search(p, text, flags=re.IGNORECASE):
            return True
    return False


def classify_intent_rules(query: str) -> Intent:
    """Fast rule-based intent classifier for a user query.

    Returns one of six intent labels. No LLM calls, no external deps.
    Order of checks is deliberate — more specific intents win first:

        decoy_resist > weighs > recent > anchor_family > opener > cold_open

    `decoy_resist` beats `anchor_family` so that "tell me something warm
    about Alex's mom" routes to decoy_resist (emotion-ballast protection),
    not to anchor_family (which would surface grief).

    `weighs` beats `recent` so that "what's been weighing on him lately"
    keeps the sentiment filter instead of becoming pure freshness.

    An empty or whitespace-only string falls through to cold_open.
    """
    if not query or not query.strip():
        return "cold_open"

    q = query.strip()

    if _match_any(q, _DECOY_PATTERNS):
        return "decoy_resist"

    if _match_any(q, _WEIGHS_PATTERNS):
        return "weighs"

    if _match_any(q, _RECENT_PATTERNS):
        return "recent"

    if _match_any(q, _ANCHOR_FAMILY_PATTERNS):
        return "anchor_family"

    if _match_any(q, _OPENER_PATTERNS):
        return "opener"

    return "cold_open"


# ---------------------------------------------------------------------------
# LLM-based classifier (opt-in safety net)
# ---------------------------------------------------------------------------

_VALID_INTENTS: tuple[str, ...] = (
    "recent",
    "weighs",
    "anchor_family",
    "opener",
    "decoy_resist",
    "cold_open",
)

INTENT_TOOL = {
    "name": "classify_query_intent",
    "description": (
        "Classify the conversation query into one of 6 intents that drive "
        "memory retrieval strategy"
    ),
    "input_schema": {
        "type": "object",
        "required": ["intent", "reason"],
        "properties": {
            "intent": {
                "type": "string",
                "enum": list(_VALID_INTENTS),
                "description": (
                    "One of the 6 intents. See the instruction prompt for "
                    "disambiguation."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "One short sentence naming which feature of the query "
                    "drove the classification. For logging only."
                ),
            },
        },
    },
}

INTENT_CLASSIFIER_PROMPT = """\
You classify conversational queries for a personal-AI-companion memory system.
Your classification drives WHICH strategy the retriever uses to pick memories.

Intents:
- recent: query asks about freshness, recent events, "last week", "lately"
  ("How's Alex these days?", "Что нового?", "What have you been up to?")
- weighs: query asks what's heavy, hard, bothering, weighing emotionally
  ("What's hard for Alex right now?", "Что тебя беспокоит?", "What weighs?")
- anchor_family: query explicitly asks about family, relationships, parents
  ("Tell me about Alex's mom", "Расскажи про его семью", "His brother?")
- opener: casual greeting, general check-in, no specific topic
  ("How are you?", "Hi", "Как дела?", "Привет")
- decoy_resist: query asks for lightness / warmth / positive, hinting that
  the retriever should DEMOTE grief anchors to preserve the mood. Tell-tale
  words: warm, happy, fun, light, something nice; or "cheer up", "поддержи",
  "что-нибудь хорошее".
- cold_open: general "bring me into context" / "what's important" without
  specific emotional direction. This is the DEFAULT. When uncertain, prefer
  cold_open over guessing. Better to fall back to the flagged-first baseline
  than to misroute to weighs or anchor_family.

Call the classify_query_intent tool exactly once. Choose the single best intent.
If two intents plausibly apply, pick the more specific one (e.g. anchor_family
over opener).
"""


def classify_intent_llm(
    query: str,
    client=None,
    model: str = "claude-sonnet-4-6",
) -> Intent:
    """LLM-based intent classifier using Claude Sonnet tool-use.

    Accepts an optional `client` so tests can inject a mock. If `client` is
    None, creates one from `ANTHROPIC_API_KEY`.

    Uses strict tool-use (like triage in `pulse_extract.py`) so output is
    enforced to the 6-intent enum — no parsing of free text, no hallucinated
    intents.

    Cost: ~$0.001 per query (Sonnet, ~400 in + 50 out tokens).

    Raises:
        RuntimeError: when `client` is None and `ANTHROPIC_API_KEY` is not
            set, when the model does not call the classifier tool, or when
            the returned intent is not in the enum (defence-in-depth in case
            the SDK ever relaxes tool-schema enforcement).
    """
    if client is None:
        import anthropic  # deferred import — keeps module light
        key = os.getenv("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY required")
        client = anthropic.Anthropic(api_key=key)

    msg = client.messages.create(
        model=model,
        max_tokens=200,
        system=INTENT_CLASSIFIER_PROMPT,
        messages=[{"role": "user", "content": query}],
        tools=[INTENT_TOOL],
        tool_choice={"type": "tool", "name": "classify_query_intent"},
    )
    for block in msg.content:
        if getattr(block, "type", None) == "tool_use" and (
            getattr(block, "name", None) == "classify_query_intent"
        ):
            intent = block.input.get("intent") if isinstance(block.input, dict) else None
            if intent not in _VALID_INTENTS:
                raise RuntimeError(
                    f"LLM returned invalid intent {intent!r}; "
                    f"expected one of {_VALID_INTENTS}"
                )
            return intent  # type: ignore[return-value]
    raise RuntimeError("LLM did not call the classifier tool")
