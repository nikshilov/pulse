"""Garden-comparable LLM-judge bench on empathic-memory-corpus.

This is Task C in the 2026-04-17 bench-hardening arc: produce a number
directly comparable to Garden's April bench result (Garden = 22.00/30 under
Opus-as-judge in empathic-memory-20260414).

Unlike `run_real_eval.py` which measures Recall@k / MRR (retrieval metrics),
this runner uses the SAME rubric judges used for Garden:

    Relevance (0-10) + Specificity (0-10) + Actionability (0-10) = /30

Reference numbers (from bench/results/empathic-memory-20260414-1914.md):
    Garden       : 24.05  (averaged across all 12 judges)
    Garden (Opus): 22.00  (Opus 4.6 as single judge)
    sqlite-vec   : 16.30
    Graphiti     : ~9
    MemPalace    : ~4

Flow per test:
  1. Run Pulse retrieve_context() → get top-k entities
  2. Pull their associated memories (event texts + facts linked to those entities)
  3. Take top-3 memories (highest-salience events from top-ranked entities)
  4. Build judge prompt using the corpus's test.fail_modes + ideal explanation
  5. Ask judge (Opus / Sonnet / GPT-4o / Gemini) to score Pulse per rubric
  6. Parse JSON → {S01_rel, S01_spec, S01_act}

Aggregate: sum(rel+spec+act) / tests = /30 score.

Cost per run (5 tests, 1 system, Opus):
  ~3k input + ~200 output tokens × 5 tests × Opus ($15 in / $75 out / 1M)
  = ~$0.30/run. With --compare (2 systems): ~$0.60/run.

Cross-judge (--cross-judge) cost: ~$1-2 per run (hybrid retrieval × 4 judges).

Usage:
    export OPENAI_API_KEY="..."     # for semantic mode + GPT judges
    export ANTHROPIC_API_KEY="..."  # for Claude judges
    export GEMINI_API_KEY="..."     # for Gemini judge

    python scripts/bench/run_llm_judge.py                          # keyword only
    python scripts/bench/run_llm_judge.py --semantic               # hybrid only
    python scripts/bench/run_llm_judge.py --compare                # both, side-by-side
    python scripts/bench/run_llm_judge.py --judge-model gpt-4o     # single non-default judge
    python scripts/bench/run_llm_judge.py --cross-judge            # 4 judges, aggregated
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from bench.run_real_eval import (  # noqa: E402
    DEFAULT_CORPUS_PATH,
    events_to_entity_gt,
    fresh_db,
    ingest_corpus,
)
from extract.intent import (  # noqa: E402
    classify_intent_llm,
    classify_intent_rules,
)
from extract.retrieval import retrieve_context  # noqa: E402
from pulse_consolidate import embed_entities  # noqa: E402


# Keywords used by rank_memories_by_intent's anchor_family filter. Mirror of
# _ANCHOR_FAMILY_PATTERNS but at memory-text level, where we can be simpler
# (substring rather than regex) and faster.
_FAMILY_TOKENS = (
    "family", "mom", "mum", "mother", "dad", "father", "parent",
    "brother", "sister", "sibling", "son", "daughter",
    "wife", "husband", "spouse", "fiancé", "fiancee", "fiance",
    "partner",
    "семь", "мам", "мать", "пап", "отец", "брат", "сестр",
    "сын", "дочь", "дочк", "жен", "муж", "родител",
)


JUDGE_MODEL = "claude-opus-4-6"
JUDGE_PROMPT_PATH = Path(
    os.path.expanduser("~/dev/ai/bench/prompts/judge-en.txt")
)


# Cross-judge panel — April 2026 12-judge parity panel (Task F2 / 2026-04-17).
# Ordering matters for the output table (grouped by provider family).
CROSS_JUDGE_MODELS: list[str] = [
    # Anthropic family
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
    # OpenAI family
    "gpt-4o",
    "gpt-5-mini",
    "gpt-4o-mini",
    # Google family
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    # OpenAI-compatible third-party providers
    "grok-4",
    "glm-4.6",
    "qwen-max",
    "kimi-k2-0905-preview",
]


# Secret-file fallbacks (env vars take precedence).
_SECRET_PATHS = {
    "ANTHROPIC_API_KEY": Path.home() / ".openclaw/secrets/anthropic-api-key.txt",
    "OPENAI_API_KEY": Path.home() / ".openclaw/secrets/openai.txt",
    "GEMINI_API_KEY": Path.home() / ".openclaw/secrets/gemini-key.txt",
    "GROK_API_KEY": Path.home() / ".openclaw/secrets/grok-api-key.txt",
    "ZAI_API_KEY": Path.home() / ".openclaw/secrets/zai-api-key.txt",
    "QWEN_API_KEY": Path.home() / ".openclaw/secrets/qwen-api-key.txt",
    "KIMI_API_KEY": Path.home() / ".openclaw/secrets/kimi-api-key.txt",
}


# OpenAI-compatible third-party endpoints (same chat.completions shape).
# Used by grok-*, glm-*, qwen-*, kimi-*/moonshot-*. Base URLs verified
# 2026-04-17 via dry-run probes against each provider.
_OPENAI_COMPAT_PROVIDERS = {
    "grok": {
        "base_url": "https://api.x.ai/v1",
        "env_var": "GROK_API_KEY",
    },
    "glm": {
        "base_url": "https://api.z.ai/api/paas/v4",
        "env_var": "ZAI_API_KEY",
    },
    "qwen": {
        "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "env_var": "QWEN_API_KEY",
    },
    "kimi": {
        "base_url": "https://api.moonshot.ai/v1",
        "env_var": "KIMI_API_KEY",
    },
    "moonshot": {
        "base_url": "https://api.moonshot.ai/v1",
        "env_var": "KIMI_API_KEY",
    },
}


# Per-call timeout (seconds). Some third-party providers (grok, glm) can be slow.
_JUDGE_CALL_TIMEOUT = 60.0

# Retry-once transient error substrings.
_TRANSIENT_ERRORS = ("timeout", "timed out", "503", "502", "504",
                     "connection reset", "connection aborted", "overloaded")


def _load_key(env_var: str) -> str | None:
    """Return the API key from env, or from the secret file on disk.

    Env var takes precedence. For the OpenAI file the format is
    ``OPENAI_API = sk-...`` (key=value, parsed with awk-ish split on ``=``).
    Anthropic + Gemini files hold the raw key on a single line.
    """
    val = os.getenv(env_var)
    if val:
        return val.strip()
    path = _SECRET_PATHS.get(env_var)
    if path is None or not path.exists():
        return None
    raw = path.read_text().strip()
    if "=" in raw and raw.splitlines()[0].split("=")[0].strip().isidentifier():
        # key = value format (OpenAI file)
        _, _, value = raw.partition("=")
        return value.strip() or None
    return raw or None


def _anthropic_client():
    """Lazy Anthropic client. Raises if key missing."""
    import anthropic  # deferred import
    key = _load_key("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY required for llm-judge runner")
    return anthropic.Anthropic(api_key=key)


def _strip_code_fence(text: str) -> str:
    """Strip optional ```json ... ``` fence. Mirrors the tolerance of the
    original `_judge_one` so all provider paths handle the same outputs.
    """
    t = text.strip()
    if not t.startswith("```"):
        return t
    # ```json\n...\n``` or ```\n...\n```
    inner = t.strip("`")
    # Drop optional language hint on the first line
    if "\n" in inner:
        first, _, rest = inner.partition("\n")
        if first.strip().lower() in {"json", ""}:
            inner = rest
    # Drop trailing backticks / fence remnant
    inner = inner.rstrip("`").strip()
    return inner


def _parse_judge_json(text: str) -> dict:
    """Parse a judge response, tolerating code fences and trailing prose."""
    cleaned = _strip_code_fence(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Last-ditch: find the first {...} block.
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            snippet = cleaned[start : end + 1]
            return json.loads(snippet)
        raise


def _is_transient(ex: Exception) -> bool:
    """Heuristic: the error looks retriable (timeout / 5xx / reset)."""
    msg = str(ex).lower()
    return any(tok in msg for tok in _TRANSIENT_ERRORS)


def _with_retry(fn, *args, **kwargs):
    """Call `fn(*args, **kwargs)`; if it raises a transient error, retry once."""
    try:
        return fn(*args, **kwargs)
    except Exception as ex:  # noqa: BLE001
        if _is_transient(ex):
            return fn(*args, **kwargs)
        raise


def _claude_judge(model: str, system_prompt: str, user_msg: str) -> dict:
    import anthropic  # deferred import
    key = _load_key("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY required for Claude judge")
    client = anthropic.Anthropic(api_key=key, timeout=_JUDGE_CALL_TIMEOUT)

    def _do():
        return client.messages.create(
            model=model,
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )

    resp = _with_retry(_do)
    text = ""
    for block in resp.content:
        if block.type == "text":
            text += block.text
    try:
        return _parse_judge_json(text)
    except json.JSONDecodeError as ex:
        raise RuntimeError(
            f"{model} returned non-JSON: {ex}\n---\n{text[:500]}"
        )


def _openai_judge(model: str, system_prompt: str, user_msg: str,
                  *, base_url: str | None = None,
                  api_key: str | None = None,
                  response_format_json: bool = True) -> dict:
    """Chat completions via any OpenAI-compatible endpoint.

    When `base_url` / `api_key` are None, uses OpenAI itself (default endpoint,
    OPENAI_API_KEY). Used by `_openai_compatible_judge` with a specific provider's
    base_url + key for grok/glm/qwen/kimi routing.

    - `response_format_json` — pass `{"type": "json_object"}`. OpenAI's own
      chat.completions supports this; some third-party compats may reject it
      (we fall back silently if the server refuses).
    - GPT-5 family uses `max_completion_tokens` instead of `max_tokens`.
    """
    from openai import OpenAI  # deferred import
    if api_key is None:
        api_key = _load_key("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("API key missing for OpenAI-compatible judge")
    client = OpenAI(api_key=api_key, base_url=base_url,
                    timeout=_JUDGE_CALL_TIMEOUT)

    # GPT-5 family requires max_completion_tokens rather than max_tokens.
    use_completion_tokens = model.startswith("gpt-5")
    token_kw = "max_completion_tokens" if use_completion_tokens else "max_tokens"
    # gpt-5-mini with reasoning can burn its output budget on hidden thinking;
    # give it a generous ceiling so there's budget left for the JSON.
    token_budget = 4096 if use_completion_tokens else 1024

    kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        token_kw: token_budget,
    }
    if response_format_json:
        kwargs["response_format"] = {"type": "json_object"}

    def _do():
        return client.chat.completions.create(**kwargs)

    try:
        resp = _with_retry(_do)
    except Exception as ex:  # noqa: BLE001
        # Some providers reject response_format; retry once without it.
        if response_format_json and "response_format" in str(ex).lower():
            kwargs.pop("response_format", None)
            resp = _with_retry(_do)
        else:
            raise

    text = resp.choices[0].message.content or ""
    # Empty-string rescue: some providers (notably GLM) accept
    # response_format=json_object but return "" for long prompts. If we asked
    # for JSON and got empty, retry once with response_format dropped.
    if not text.strip() and response_format_json:
        kwargs.pop("response_format", None)
        resp = _with_retry(_do)
        text = resp.choices[0].message.content or ""
    try:
        return _parse_judge_json(text)
    except json.JSONDecodeError as ex:
        raise RuntimeError(
            f"{model} returned non-JSON: {ex}\n---\n{text[:500]}"
        )


def _openai_compatible_judge(model: str, system_prompt: str, user_msg: str,
                             *, provider_key: str) -> dict:
    """Dispatch to an OpenAI-compatible third-party (grok/glm/qwen/kimi).

    Reads `_OPENAI_COMPAT_PROVIDERS[provider_key]` for base_url and env_var,
    loads the key, and delegates to `_openai_judge` with the right plumbing.

    Third-party compats are less likely to honor `response_format=json_object`;
    we pass it but fall back on error (handled inside _openai_judge).
    """
    cfg = _OPENAI_COMPAT_PROVIDERS.get(provider_key)
    if cfg is None:
        raise ValueError(f"unknown OpenAI-compatible provider: {provider_key}")
    key = _load_key(cfg["env_var"])
    if not key:
        raise RuntimeError(
            f"{cfg['env_var']} required for {provider_key} judge"
        )
    # GLM-4.6 accepts response_format=json_object without error, but returns an
    # empty string for long rubric prompts (observed 4/5 empty responses in the
    # 12-judge run 2026-04-17). Disable the flag for glm specifically; the
    # system-prompt already instructs "return ONLY JSON" and our parser strips
    # any accidental code fences / trailing prose.
    #
    # Grok / Qwen / Kimi (Moonshot) handle response_format fine; keep it on —
    # _openai_judge retries without if any server refuses.
    response_format_json = provider_key != "glm"
    return _openai_judge(
        model, system_prompt, user_msg,
        base_url=cfg["base_url"], api_key=key,
        response_format_json=response_format_json,
    )


def _gemini_judge(model: str, system_prompt: str, user_msg: str) -> dict:
    try:
        import google.generativeai as genai  # deferred import
    except ImportError as ex:
        raise RuntimeError(
            "google-generativeai package not installed; pip install it to use Gemini judges"
        ) from ex
    key = _load_key("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY required for Gemini judge")
    genai.configure(api_key=key)
    model_obj = genai.GenerativeModel(model)
    # Gemini doesn't have a native system prompt for generate_content; prepend.
    prompt = system_prompt + "\n\n" + user_msg

    def _do():
        return model_obj.generate_content(
            prompt,
            request_options={"timeout": _JUDGE_CALL_TIMEOUT},
        )

    response = _with_retry(_do)
    text = getattr(response, "text", None) or ""
    try:
        return _parse_judge_json(text)
    except json.JSONDecodeError as ex:
        raise RuntimeError(
            f"{model} returned non-JSON: {ex}\n---\n{text[:500]}"
        )


def judge_call(model: str, system_prompt: str, user_msg: str) -> dict:
    """Provider-agnostic judge call. Routes by model prefix.

    Returns the parsed JSON score dict (per-system `S01_rel` etc. + `note`).

    Prefix routing (order matters — more specific prefixes first):
      - claude-*                  → Anthropic Messages API
      - gpt-*                     → OpenAI chat.completions
      - gemini-*                  → Google Generative AI
      - grok-*                    → xAI (OpenAI-compat)
      - glm-*                     → z.ai (OpenAI-compat)
      - qwen-*                    → Alibaba DashScope International (OpenAI-compat)
      - kimi-* / moonshot-*       → Moonshot (OpenAI-compat)
    """
    if model.startswith("claude-"):
        return _claude_judge(model, system_prompt, user_msg)
    if model.startswith("gpt-"):
        return _openai_judge(model, system_prompt, user_msg)
    if model.startswith("gemini-"):
        return _gemini_judge(model, system_prompt, user_msg)
    if model.startswith("grok-"):
        return _openai_compatible_judge(model, system_prompt, user_msg,
                                        provider_key="grok")
    if model.startswith("glm-"):
        return _openai_compatible_judge(model, system_prompt, user_msg,
                                        provider_key="glm")
    if model.startswith("qwen-") or model.startswith("qwen3-"):
        return _openai_compatible_judge(model, system_prompt, user_msg,
                                        provider_key="qwen")
    if model.startswith("kimi-") or model.startswith("moonshot-"):
        return _openai_compatible_judge(model, system_prompt, user_msg,
                                        provider_key="kimi")
    raise ValueError(f"unknown judge model: {model}")


def _gemini_available() -> bool:
    """True iff google-generativeai is importable AND GEMINI_API_KEY resolvable."""
    try:
        import google.generativeai  # noqa: F401
    except ImportError:
        return False
    return _load_key("GEMINI_API_KEY") is not None


def _pull_top_memories(con, entity_ids: list[int], corpus_events: list[dict],
                      max_memories: int = 3, intent: str = "cold_open") -> list[dict]:
    """Collect top-k memories for a list of retrieved entities.

    A "memory" here is the text of an event the entity participates in.
    We collect all candidate event-memories for the retrieved entities,
    then delegate to `rank_memories_by_intent` to choose ordering.
    """
    if not entity_ids:
        return []
    placeholders = ",".join("?" * len(entity_ids))
    rows = con.execute(
        f"SELECT DISTINCT e.id, e.title, e.description, e.sentiment "
        f"FROM events e "
        f"JOIN event_entities ee ON ee.event_id = e.id "
        f"WHERE ee.entity_id IN ({placeholders})",
        tuple(entity_ids),
    ).fetchall()
    # Match back to corpus for user_flag + days_ago
    by_id = {ev["id"]: ev for ev in corpus_events}
    pool: list[dict] = []
    for (eid, title, description, sentiment) in rows:
        cev = by_id.get(eid, {})
        pool.append({
            "id": eid,
            "text": description or title,
            "sentiment": sentiment or 0,
            "user_flag": 1 if cev.get("user_flag") else 0,
            "days_ago": cev.get("days_ago"),
        })
    ranked = rank_memories_by_intent(pool, intent)
    return ranked[:max_memories]


def _mentions_family(text: str) -> bool:
    """True if a memory's text references a family member / relationship."""
    if not text:
        return False
    low = text.lower()
    return any(tok in low for tok in _FAMILY_TOKENS)


def rank_memories_by_intent(memories: list[dict], intent: str) -> list[dict]:
    """Re-rank a pool of event-memories according to query intent.

    Each memory dict should have: id, text, sentiment, user_flag, days_ago.
    Returns the full pool sorted by intent-appropriate key. The caller
    slices to `max_memories`.

    Strategy table:

    | intent         | ordering                                               |
    |----------------|--------------------------------------------------------|
    | recent         | days_ago ASC, user_flag DESC                           |
    | weighs         | filter sentiment<0 → abs(sentiment) DESC, days_ago ASC;|
    |                | pad with remaining by abs(sentiment) DESC if <3        |
    | anchor_family  | filter text mentions family → user_flag DESC,          |
    |                | abs(sentiment) DESC; pad with user_flag=1 globals      |
    | opener         | user_flag DESC, abs(sentiment) DESC                    |
    | decoy_resist   | sentiment DESC, days_ago ASC (positives first);        |
    |                | preserve one user_flag as safety anchor in slot 2/3    |
    | cold_open      | user_flag DESC, abs(sentiment) DESC (baseline)         |
    """
    if not memories:
        return []

    def by_recent(m: dict) -> tuple:
        # Freshness-biased blend: fresher wins, but neutral mundane events
        # should not beat a slightly older emotionally-weighted event.
        # Primary key: days_ago penalized by emotional magnitude and flag.
        # A flagged item effectively gets a ~30-day freshness bonus; each
        # |sentiment| point gets a ~7-day bonus. This keeps "lately" queries
        # from surfacing neutral gym/tacos events over recent weddings.
        days = m.get("days_ago")
        if days is None:
            days = 10**9
        sent_mag = abs(m.get("sentiment", 0) or 0)
        flag = int(m.get("user_flag", 0))
        adjusted = days - 7 * sent_mag - 30 * flag
        return (adjusted, days)

    def by_weight(m: dict) -> tuple:
        return (-abs(m.get("sentiment", 0) or 0),
                m.get("days_ago") if m.get("days_ago") is not None else 10**9)

    def by_flag_then_weight(m: dict) -> tuple:
        return (-int(m.get("user_flag", 0)),
                -abs(m.get("sentiment", 0) or 0))

    if intent == "recent":
        return sorted(memories, key=by_recent)

    if intent == "weighs":
        negatives = [m for m in memories if (m.get("sentiment") or 0) < 0]
        others = [m for m in memories if (m.get("sentiment") or 0) >= 0]
        negatives_sorted = sorted(negatives, key=by_weight)
        others_sorted = sorted(others, key=by_weight)
        # Pad with non-negatives only if we have too few negatives for slot-3.
        if len(negatives_sorted) >= 3:
            return negatives_sorted + others_sorted
        return negatives_sorted + others_sorted

    if intent == "anchor_family":
        family = [m for m in memories if _mentions_family(m.get("text", ""))]
        other = [m for m in memories if not _mentions_family(m.get("text", ""))]
        family_sorted = sorted(family, key=by_flag_then_weight)
        # Pad with globally user-flagged entries not already in family set.
        family_ids = {m["id"] for m in family_sorted}
        padding_flagged = sorted(
            [m for m in other if m.get("user_flag") and m["id"] not in family_ids],
            key=by_flag_then_weight,
        )
        padding_rest = sorted(
            [m for m in other if not m.get("user_flag") and m["id"] not in family_ids],
            key=by_flag_then_weight,
        )
        return family_sorted + padding_flagged + padding_rest

    if intent == "opener":
        return sorted(memories, key=by_flag_then_weight)

    if intent == "decoy_resist":
        # Positive-first, recency-tiebreak. Then reserve one user_flag slot
        # as a safety-anchor (slot 2 if 2+ items, else append).
        positives = [m for m in memories if (m.get("sentiment") or 0) > 0]
        neutrals = [m for m in memories if (m.get("sentiment") or 0) == 0]
        negatives = [m for m in memories if (m.get("sentiment") or 0) < 0]

        def by_positive(m: dict) -> tuple:
            return (-1 * (m.get("sentiment") or 0),
                    m.get("days_ago") if m.get("days_ago") is not None
                    else 10**9)

        pos_sorted = sorted(positives, key=by_positive)
        neu_sorted = sorted(neutrals, key=by_positive)
        neg_sorted = sorted(
            negatives,
            key=lambda m: (-int(m.get("user_flag", 0)),
                           -abs(m.get("sentiment") or 0)),
        )

        base = pos_sorted + neu_sorted + neg_sorted
        # Promote the first user_flag grief anchor into slot 2 as a safety
        # warning — judges penalize surfacing grief top-1 but reward including
        # the anchor as context.
        anchor_idx = next(
            (i for i, m in enumerate(base)
             if m.get("user_flag") and (m.get("sentiment") or 0) < 0),
            None,
        )
        if anchor_idx is not None and anchor_idx > 1 and len(base) >= 2:
            anchor = base.pop(anchor_idx)
            base.insert(1, anchor)
        return base

    # cold_open — baseline behavior: user_flag first, then |sentiment|.
    return sorted(memories, key=by_flag_then_weight)


def _format_system_block(system_label: str, memories: list[dict]) -> str:
    """Render one system's top-3 memories into the judge-expected format."""
    lines = [f"{system_label}:"]
    if not memories:
        lines.append("  (no memories returned)")
        return "\n".join(lines)
    for i, m in enumerate(memories, 1):
        tag = "[FLAGGED] " if m.get("user_flag") else ""
        days = f" [{m['days_ago']}d ago]" if m.get("days_ago") is not None else ""
        lines.append(
            f"  Memory {i}: {tag}event_id={m['id']}{days} "
            f"sentiment={m['sentiment']:+.0f} — {m['text']}"
        )
    return "\n".join(lines)


def _build_judge_user_msg(test: dict, corpus_events: list[dict],
                          systems: list[tuple[str, list[dict]]]) -> str:
    """Assemble the judge's user message for one test query."""
    by_id = {ev["id"]: ev for ev in corpus_events}
    ideal_ids = test["ideal_top_3_event_ids"]
    ideal_block = "\n".join(
        f"  event {i}: {by_id.get(i, {}).get('text', '(missing)')}"
        for i in ideal_ids
    )
    fail_modes_block = "\n".join(f"  - {f}" for f in test.get("fail_modes", []))
    system_blocks = "\n\n".join(
        _format_system_block(label, mems) for (label, mems) in systems
    )

    # Emit only the slots we actually have so Opus doesn't hallucinate
    # scores for Systems 03-15. We pad with "(not run)" markers below.
    filled_slots = [label for (label, _) in systems]
    pad_lines = []
    for i in range(len(filled_slots) + 1, 16):
        pad_lines.append(f"System {i:02d}: (not run — score all dims as 0)")
    pad_block = "\n".join(pad_lines)

    return (
        f"## Conversation moment\n\n"
        f"User query: {test['user_query']!r}\n"
        f"What this tests: {test.get('what_it_tests', '(not specified)')}\n\n"
        f"## Ideal top-3 memories (event IDs: {ideal_ids})\n\n"
        f"{ideal_block}\n\n"
        f"Ideal explanation: {test.get('ideal_explanation', '')}\n\n"
        f"## Failure modes to penalize\n\n"
        f"{fail_modes_block}\n\n"
        f"## Retrieved memories per system\n\n"
        f"{system_blocks}\n\n"
        f"{pad_block}\n\n"
        f"## Scoring\n\n"
        f"Score each system on rel/spec/act (0-10 each). Return ONLY the JSON "
        f"object specified in the system prompt — no prose, no code fences."
    )


def _judge_one(judge_prompt: str, test: dict, corpus_events: list[dict],
               systems: list[tuple[str, list[dict]]],
               model: str = JUDGE_MODEL) -> dict:
    """Run one judge call via the provider-agnostic dispatcher.

    Backwards compatibility: existing callers passed ``client`` as the first
    positional argument. We now ignore any such client and use `model` to
    route. To migrate cleanly we accept the old shape too via keyword.
    """
    user_msg = _build_judge_user_msg(test, corpus_events, systems)
    return judge_call(model, judge_prompt, user_msg)


def _retrieve_memories_for_test(con, test: dict, corpus_events: list[dict],
                                semantic: bool, embedder_model: str,
                                semantic_top_n: int,
                                intent: str = "cold_open") -> list[dict]:
    """Run Pulse retrieval for the test query, return top-3 event memories.

    The `intent` argument is forwarded to `_pull_top_memories`, which uses it
    to switch ranking strategy. Entity-level retrieval is unchanged.
    """
    result = retrieve_context(
        con, test["user_query"], top_k=10, depth=2,
        semantic=semantic, embedder_model=embedder_model,
        semantic_top_n=semantic_top_n,
    )
    entity_ids = [e["id"] for e in result["matched_entities"]]
    return _pull_top_memories(
        con, entity_ids, corpus_events, max_memories=3, intent=intent,
    )


def _get_intent_classifier(name: str):
    """Return a callable `(query) -> Intent` for the chosen backend.

    Supported backends:
      - "rules" (default): fast, deterministic, no API calls.
      - "llm": Claude Sonnet tool-use classifier; ~$0.001/query. Shares the
        same Anthropic client factory as the judge — if ANTHROPIC_API_KEY is
        missing, `classify_intent_llm` itself raises.
    """
    if name == "rules":
        return classify_intent_rules
    if name == "llm":
        return classify_intent_llm
    raise ValueError(
        f"unknown intent classifier {name!r}; expected 'rules' or 'llm'"
    )


def run(
    corpus_path: Path = DEFAULT_CORPUS_PATH,
    semantic: bool = False,
    embedder_model: str = "openai-text-embedding-3-large",
    semantic_top_n: int = 3,
    verbose: bool = False,
    intent_classifier: str = "rules",
    judge_model: str = JUDGE_MODEL,
) -> dict:
    corpus = json.loads(Path(corpus_path).read_text())
    con = fresh_db()
    ingest_corpus(con, corpus)
    if semantic:
        embed_entities(con, embedder_model=embedder_model, only_missing=True)

    judge_prompt = JUDGE_PROMPT_PATH.read_text()
    mode = "hybrid" if semantic else "keyword"
    system_label = f"System 01 (Pulse {mode}, intent={intent_classifier})"
    classify = _get_intent_classifier(intent_classifier)

    per_test: list[dict] = []
    for test in corpus["tests"]:
        intent = classify(test["user_query"])
        memories = _retrieve_memories_for_test(
            con, test, corpus["events"],
            semantic=semantic, embedder_model=embedder_model,
            semantic_top_n=semantic_top_n,
            intent=intent,
        )
        verdict = _judge_one(
            judge_prompt, test, corpus["events"],
            systems=[(system_label, memories)],
            model=judge_model,
        )
        rel = verdict.get("S01_rel", 0)
        spec = verdict.get("S01_spec", 0)
        act = verdict.get("S01_act", 0)
        total = rel + spec + act
        per_test.append({
            "test_id": test["id"],
            "name": test["name"],
            "intent": intent,
            "rel": rel, "spec": spec, "act": act, "total": total,
            "note": verdict.get("note", ""),
            "memories": memories,
        })
        if verbose:
            print(f"[{test['id']}] {test['name']}  intent={intent}  "
                  f"rel={rel} spec={spec} act={act} total={total}/30")
            print(f"  note: {verdict.get('note', '')}")

    mean_total = sum(t["total"] for t in per_test) / len(per_test) if per_test else 0
    mean_rel = sum(t["rel"] for t in per_test) / len(per_test) if per_test else 0
    mean_spec = sum(t["spec"] for t in per_test) / len(per_test) if per_test else 0
    mean_act = sum(t["act"] for t in per_test) / len(per_test) if per_test else 0

    return {
        "mode": mode,
        "judge_model": judge_model,
        "mean_total": mean_total,
        "mean_rel": mean_rel,
        "mean_spec": mean_spec,
        "mean_act": mean_act,
        "per_test": per_test,
    }


def run_cross_judge(
    corpus_path: Path = DEFAULT_CORPUS_PATH,
    embedder_model: str = "openai-text-embedding-3-large",
    semantic_top_n: int = 3,
    verbose: bool = False,
    judges: list[str] | None = None,
) -> dict:
    """Cross-judge validation. Hybrid retrieval runs ONCE, then each judge
    scores the SAME per-test (test, memories) pair independently.

    Returns::

        {
            "judges": [{"model": str, "mean_rel": ..., "mean_total": ...,
                        "per_test": [...]}, ...],
            "mean_total_across_judges": float,
            "stddev_total_across_judges": float,
            ...
        }
    """
    import math

    corpus = json.loads(Path(corpus_path).read_text())
    con = fresh_db()
    ingest_corpus(con, corpus)
    embed_entities(con, embedder_model=embedder_model, only_missing=True)

    judge_prompt = JUDGE_PROMPT_PATH.read_text()
    system_label = "System 01 (Pulse hybrid)"

    # Resolve judge panel. Skip judges gracefully if their SDK/key is missing;
    # remaining auth / quota / bad-model-ID failures are caught later at the
    # per-call site so one dead judge never aborts the whole run.
    panel = list(judges) if judges else list(CROSS_JUDGE_MODELS)
    resolved: list[str] = []
    skipped_preflight: list[tuple[str, str]] = []  # (model, reason)
    for m in panel:
        if m.startswith("gemini-") and not _gemini_available():
            reason = "google-generativeai missing or GEMINI_API_KEY unresolvable"
            print(f"WARN: {m} skipped — {reason}", file=sys.stderr)
            skipped_preflight.append((m, reason))
            continue
        # Third-party compat providers: check key existence up-front.
        compat_check = None
        for prefix, provider in (
            ("grok-", "grok"),
            ("glm-", "glm"),
            ("qwen-", "qwen"),
            ("qwen3-", "qwen"),
            ("kimi-", "kimi"),
            ("moonshot-", "moonshot"),
        ):
            if m.startswith(prefix):
                compat_check = provider
                break
        if compat_check:
            cfg = _OPENAI_COMPAT_PROVIDERS[compat_check]
            if not _load_key(cfg["env_var"]):
                reason = f"{cfg['env_var']} unresolvable"
                print(f"WARN: {m} skipped — {reason}", file=sys.stderr)
                skipped_preflight.append((m, reason))
                continue
        resolved.append(m)

    # STEP 1 — Run retrieval once per test. Memories are what the judges see.
    retrieval_rows: list[dict] = []
    for test in corpus["tests"]:
        intent = classify_intent_rules(test["user_query"])
        memories = _retrieve_memories_for_test(
            con, test, corpus["events"],
            semantic=True, embedder_model=embedder_model,
            semantic_top_n=semantic_top_n, intent=intent,
        )
        retrieval_rows.append({"test": test, "intent": intent, "memories": memories})

    # STEP 2 — Each judge scores every test.
    judge_results: list[dict] = []
    for model in resolved:
        if verbose:
            print(f"\n>>> Judging with {model}")
        per_test: list[dict] = []
        errors: list[str] = []
        for row in retrieval_rows:
            test = row["test"]
            try:
                verdict = _judge_one(
                    judge_prompt, test, corpus["events"],
                    systems=[(system_label, row["memories"])],
                    model=model,
                )
            except Exception as ex:  # noqa: BLE001
                errors.append(f"{test['id']}: {ex}")
                if verbose:
                    print(f"  ERROR [{test['id']}]: {ex}")
                per_test.append({
                    "test_id": test["id"], "name": test["name"],
                    "intent": row["intent"],
                    "rel": 0, "spec": 0, "act": 0, "total": 0,
                    "note": f"ERROR: {ex}", "memories": row["memories"],
                    "failed": True,
                })
                continue
            rel = verdict.get("S01_rel", 0)
            spec = verdict.get("S01_spec", 0)
            act = verdict.get("S01_act", 0)
            total = rel + spec + act
            per_test.append({
                "test_id": test["id"], "name": test["name"],
                "intent": row["intent"],
                "rel": rel, "spec": spec, "act": act, "total": total,
                "note": verdict.get("note", ""), "memories": row["memories"],
            })
            if verbose:
                print(f"  [{test['id']}] {test['name']}  "
                      f"rel={rel} spec={spec} act={act} total={total}/30")

        valid = [t for t in per_test if not t.get("failed")]
        all_failed = len(valid) == 0
        n = len(valid) or 1
        judge_results.append({
            "model": model,
            "mean_rel": sum(t["rel"] for t in valid) / n,
            "mean_spec": sum(t["spec"] for t in valid) / n,
            "mean_act": sum(t["act"] for t in valid) / n,
            "mean_total": sum(t["total"] for t in valid) / n,
            "per_test": per_test,
            "errors": errors,
            "fully_failed": all_failed,
        })
        if all_failed and errors:
            # Surface this immediately — the judge will be dropped from the mean.
            print(
                f"WARN: {model} failed on all {len(errors)} tests; "
                f"dropping from cross-judge mean. First error: {errors[0]}",
                file=sys.stderr,
            )

    # STEP 3 — Aggregate across judges, dropping any that fully failed.
    scoring_judges = [j for j in judge_results if not j.get("fully_failed")]
    totals = [j["mean_total"] for j in scoring_judges]
    rels = [j["mean_rel"] for j in scoring_judges]
    specs = [j["mean_spec"] for j in scoring_judges]
    acts = [j["mean_act"] for j in scoring_judges]
    n = len(totals) or 1
    mean = sum(totals) / n
    if len(totals) > 1:
        var = sum((t - mean) ** 2 for t in totals) / (len(totals) - 1)
        stddev = math.sqrt(var)
    else:
        stddev = 0.0

    return {
        "judges": judge_results,
        "skipped_preflight": skipped_preflight,
        "n_scoring_judges": len(scoring_judges),
        "n_panel": len(panel),
        "mean_rel_across_judges": sum(rels) / n if rels else 0.0,
        "mean_spec_across_judges": sum(specs) / n if specs else 0.0,
        "mean_act_across_judges": sum(acts) / n if acts else 0.0,
        "mean_total_across_judges": mean,
        "stddev_total_across_judges": stddev,
    }


def _short_judge_name(model: str) -> str:
    """Shorter display name for table columns."""
    mapping = {
        "claude-opus-4-6": "claude-opus-4-6",
        "claude-sonnet-4-6": "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001": "claude-haiku-4-5",
        "gpt-4o": "gpt-4o",
        "gpt-5-mini": "gpt-5-mini",
        "gpt-4o-mini": "gpt-4o-mini",
        "gemini-2.5-pro": "gemini-2.5-pro",
        "gemini-2.5-flash": "gemini-2.5-flash",
        "grok-4": "grok-4",
        "glm-4.6": "glm-4.6",
        "qwen-max": "qwen-max",
        "kimi-k2-0905-preview": "kimi-k2",
    }
    return mapping.get(model, model)


def _print_cross_judge_result(r: dict) -> None:
    print()
    print("=" * 78)
    print("PULSE hybrid — CROSS-JUDGE validation on empathic-memory-corpus")
    print("=" * 78)
    print()
    header = f"{'JUDGE':<24} {'Rel':>6} {'Spec':>6} {'Act':>6} {'Total /30':>12}"
    print(header)
    print("-" * len(header))
    for j in r["judges"]:
        label = _short_judge_name(j["model"])
        if j.get("fully_failed"):
            print(f"{label:<24} {'—':>6} {'—':>6} {'—':>6} {'FAILED':>12}")
        else:
            print(f"{label:<24} "
                  f"{j['mean_rel']:>6.2f} {j['mean_spec']:>6.2f} "
                  f"{j['mean_act']:>6.2f} {j['mean_total']:>12.2f}")
    print("-" * len(header))
    print(f"{'MEAN ± STDDEV':<24} "
          f"{r['mean_rel_across_judges']:>6.2f} "
          f"{r['mean_spec_across_judges']:>6.2f} "
          f"{r['mean_act_across_judges']:>6.2f} "
          f"{r['mean_total_across_judges']:>7.2f} ± {r['stddev_total_across_judges']:.2f}")
    print()

    scoring = r.get("n_scoring_judges", len(r["judges"]))
    panel = r.get("n_panel", len(r["judges"]))
    print(f"Judges scoring: {scoring} / {panel} (panel)")
    skipped_pre = r.get("skipped_preflight", [])
    if skipped_pre:
        print("Skipped pre-flight:")
        for (m, reason) in skipped_pre:
            print(f"  - {m}: {reason}")
    failed = [j for j in r["judges"] if j.get("fully_failed")]
    if failed:
        print("Failed during run:")
        for j in failed:
            first = j["errors"][0] if j["errors"] else "(no error recorded)"
            print(f"  - {j['model']}: {first[:140]}")
    print()
    print("Reference (April 2026, 12 judges averaged):")
    print("  Garden                                              24.05")
    print("  sqlite-vec                                         ~16.30")
    print("  Graphiti                                            ~9.00")
    print()


def _print_result(r: dict) -> None:
    print()
    print("=" * 78)
    print(f"PULSE ({r['mode']}) — LLM judge, Opus-4.6, empathic-memory-corpus")
    print("=" * 78)
    print(f"  Relevance      : {r['mean_rel']:.2f} / 10")
    print(f"  Specificity    : {r['mean_spec']:.2f} / 10")
    print(f"  Actionability  : {r['mean_act']:.2f} / 10")
    print(f"  TOTAL          : {r['mean_total']:.2f} / 30")
    print()
    print("Reference (empathic-memory-20260414 bench, same rubric, Opus judge):")
    print("  Garden         : 22.00 / 30")
    print("  sqlite-vec     : ~15.00 / 30")
    print("  Graphiti       : ~9.00 / 30")
    print("  MemPalace      : ~4.00 / 30")
    print()


def main() -> int:
    p = argparse.ArgumentParser(
        description="Pulse retrieval scored by LLM judges, Garden-comparable."
    )
    p.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_PATH)
    p.add_argument("--semantic", action="store_true",
                   help="enable hybrid retrieval (keyword + OpenAI embeddings)")
    p.add_argument("--embedder-model", default="openai-text-embedding-3-large",
                   choices=["fake-local", "openai-text-embedding-3-large"])
    p.add_argument("--semantic-top-n", type=int, default=3)
    p.add_argument("--compare", action="store_true",
                   help="run both keyword and hybrid, print side-by-side")
    p.add_argument(
        "--intent-classifier",
        default="rules",
        choices=["rules", "llm"],
        help=(
            "which intent classifier to use per test query. "
            "'rules' (default): fast, deterministic, zero API cost. "
            "'llm': Claude Sonnet tool-use, ~$0.001/query — safety net for "
            "queries the rules miss (indirect phrasing, irony, mixed lang)."
        ),
    )
    p.add_argument("--judge-model", default=JUDGE_MODEL,
                   help="which single judge to use (e.g. claude-opus-4-6, "
                        "claude-sonnet-4-6, gpt-4o, gemini-2.5-pro). "
                        "Ignored when --cross-judge is set.")
    p.add_argument("--cross-judge", action="store_true",
                   help="run Pulse hybrid once, score with the full 12-judge "
                        "panel (Anthropic Opus/Sonnet/Haiku, OpenAI "
                        "GPT-4o/GPT-5-mini/GPT-4o-mini, Google Gemini "
                        "Pro/Flash, xAI Grok-4, z.ai GLM-4.6, Qwen-Max, "
                        "Moonshot Kimi-K2), print aggregated table "
                        "(implies --semantic)")
    p.add_argument("--json-out", type=Path, default=None,
                   help="if set, dump the full cross-judge result as JSON "
                        "to this path (structured capture for automation)")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    if not args.corpus.exists():
        print(f"ERROR: corpus not found at {args.corpus}", file=sys.stderr)
        return 2

    if args.cross_judge:
        r = run_cross_judge(
            corpus_path=args.corpus,
            embedder_model=args.embedder_model,
            semantic_top_n=args.semantic_top_n,
            verbose=args.verbose,
        )
        _print_cross_judge_result(r)
        if args.json_out:
            # Drop per_test.memories (noisy object refs) before JSON dump.
            slim = {
                **r,
                "judges": [
                    {
                        **j,
                        "per_test": [
                            {k: v for k, v in t.items() if k != "memories"}
                            for t in j.get("per_test", [])
                        ],
                    }
                    for j in r["judges"]
                ],
            }
            args.json_out.write_text(json.dumps(slim, indent=2, default=str))
            print(f"JSON result written to {args.json_out}")
        return 0

    if args.compare:
        print(">>> KEYWORD")
        kw = run(
            corpus_path=args.corpus,
            semantic=False,
            verbose=args.verbose,
            intent_classifier=args.intent_classifier,
            judge_model=args.judge_model,
        )
        _print_result(kw)
        print(">>> HYBRID")
        hy = run(
            corpus_path=args.corpus,
            semantic=True,
            embedder_model=args.embedder_model,
            semantic_top_n=args.semantic_top_n,
            verbose=args.verbose,
            intent_classifier=args.intent_classifier,
            judge_model=args.judge_model,
        )
        _print_result(hy)
        print("=" * 78)
        print("DELTA:  hybrid − keyword")
        print("=" * 78)
        print(f"  Rel  : {hy['mean_rel']:.2f} − {kw['mean_rel']:.2f} "
              f"= {hy['mean_rel'] - kw['mean_rel']:+.2f}")
        print(f"  Spec : {hy['mean_spec']:.2f} − {kw['mean_spec']:.2f} "
              f"= {hy['mean_spec'] - kw['mean_spec']:+.2f}")
        print(f"  Act  : {hy['mean_act']:.2f} − {kw['mean_act']:.2f} "
              f"= {hy['mean_act'] - kw['mean_act']:+.2f}")
        print(f"  TOTAL: {hy['mean_total']:.2f} − {kw['mean_total']:.2f} "
              f"= {hy['mean_total'] - kw['mean_total']:+.2f}")
        print()
        return 0

    r = run(
        corpus_path=args.corpus,
        semantic=args.semantic,
        embedder_model=args.embedder_model,
        semantic_top_n=args.semantic_top_n,
        judge_model=args.judge_model,
        verbose=args.verbose,
        intent_classifier=args.intent_classifier,
    )
    _print_result(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
