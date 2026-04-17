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

TODO(intent-v2): add an optional LLM-based classifier as a fallback for
ambiguous queries; keep rule-based as the fast path.
"""

from __future__ import annotations

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
