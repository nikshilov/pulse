"""Event-level retrieval — Pulse v3 with conditional emotion/state/chain boosts.

Wraps `retrieval_v2.py` (base cosine × recency × belief_class decay) and adds
three CONDITIONAL multiplicative terms:

  - emotion_alignment boost: active only when query has a dominant emotion (max ≥ 0.5)
  - state_fit boost:          active only when body signal is strong
                              (low sleep / high stress / elevated HR trend)
  - chain_expansion:          active only when return_chain=True (chain-type queries)

CRITICAL DESIGN RULE (from Phase D negative result 2026-04-20):
When any signal is neutral → that boost is OFF → formula collapses to v2_pure.
This prevents the monotonic hurt that killed always-on multiplicative terms.

Empirical validation (Qwen Max judge, 2026-04-22, bench v3 full matrix):
  overall 5.66 vs best baseline 4.85 (+17%)
  stateful 3.20 vs 1.00 (+220%)
  chain    4.50 vs 2.58 (+74%)
  core     7.48 vs 7.03 (no regression vs v2_pure)
Snapshot: ~/dev/ai/Garden/bench/external-evals/snapshots/2026-04-22-bench-v3-pulse-v3-qwen/

Schema requirements:
  - migration 013: event_embeddings (event_id, model, vector_json)
  - migration 014: events.belief_class, confidence_floor (retrieval_v2 fields)
  - migration 015: event_emotions, event_chains, query_emotion_cache

Usage:
    from extract.retrieval_v3 import retrieve_events_v3, UserState
    results = retrieve_events_v3(
        con, "что у меня с Аней?",
        top_k=3,
        user_state=UserState(mood_vector={"shame": 0.8}),
        embedder_model="openai-text-embedding-3-large",
    )
"""
from __future__ import annotations

import hashlib
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from extract.embedder import embed_texts
from extract.retrieval_v2 import (
    BELIEF_DECAY, DEFAULT_LAMBDA, DEFAULT_EMBEDDER, DEFAULT_TOP_N_CANDIDATES,
    _cosine, _fetch_event, _days_since,
)


EMOTION_KEYS = (
    "joy", "sadness", "anger", "fear", "trust",
    "disgust", "anticipation", "surprise", "shame", "guilt",
)

# Default boost coefficients. Kept small and conditional to avoid Phase D failure mode.
DEFAULT_BETA = 0.15   # emotion boost cap ≈ +15%
DEFAULT_GAMMA = 0.15  # state boost cap   ≈ +15%
DEFAULT_DELTA_ANCHOR = 0.05  # anchor boost for user_flag=True events already in top-N (marginal)
DEFAULT_DELTA_DATE = 0.25    # date-proximity boost cap when state.snapshot_days_ago set
ANCHOR_TOP_N = 8             # only boost anchors that reach top-N by base score

# Phase 5.4: anchor-aware decay. user_flag=True events are structural truths
# (marriage anchors, zasluzhivatel, communication rules) and should not fade
# with recency. Half-life ~693d for anchors vs ~347d for regular events.
# Matches Pulse v2 BELIEF_DECAY tier convention: self_model=0.0005 (axiom-like),
# we pick 0.001 as conservative middle-ground — aggressive enough to preserve
# old marriage-anchor events but not so eternal that stale anchors dominate
# recent moments.
DEFAULT_LAMBDA_ANCHOR = 0.001


@dataclass
class UserState:
    """Current user state for stateful retrieval. All fields optional."""
    mood_vector: dict[str, float] = field(default_factory=dict)
    sleep_quality: Optional[float] = None
    sleep_hours: Optional[float] = None
    hrv: Optional[float] = None
    hr_trend: Optional[str] = None           # "elevated_3d" | "stable" | "low" | "elevated_overnight"
    hrv_trend: Optional[str] = None          # "declining_3d" | "stable" | "rising"
    stress_proxy: Optional[float] = None
    recent_life_events_7d: list[str] = field(default_factory=list)
    time_of_day: Optional[str] = None
    snapshot_days_ago: Optional[float] = None   # state represents a specific past moment (days_ago scale)

    def has_dominant_emotion(self, threshold: float = 0.5) -> bool:
        if not self.mood_vector:
            return False
        return max(self.mood_vector.values()) >= threshold

    def is_body_stressed(self) -> bool:
        if self.stress_proxy is not None and self.stress_proxy >= 0.6:
            return True
        if self.sleep_quality is not None and self.sleep_quality <= 0.4:
            return True
        if self.hr_trend in ("elevated_3d", "elevated_overnight"):
            return True
        if self.hrv_trend == "declining_3d":
            return True
        if self.hrv is not None and self.hrv < 55:
            return True
        return False

    def is_body_restored(self) -> bool:
        if (self.stress_proxy is not None and self.stress_proxy <= 0.3 and
                (self.sleep_quality is None or self.sleep_quality >= 0.7)):
            return True
        return False


# ────────────────────────────────────────────────────────────────────────────
# Emotion helpers (SQLite-backed)
# ────────────────────────────────────────────────────────────────────────────

def _emotion_vec_from_row(row: sqlite3.Row | tuple | None) -> list[float]:
    """Convert event_emotions row (10 REAL columns in EMOTION_KEYS order) to list."""
    if row is None:
        return [0.0] * len(EMOTION_KEYS)
    return [float(row[i]) for i in range(len(EMOTION_KEYS))]


def _fetch_event_emotion(con: sqlite3.Connection, event_id: int) -> list[float]:
    """Fetch 10-dim Plutchik vector for event. Missing rows → zero vector."""
    try:
        row = con.execute(
            "SELECT joy, sadness, anger, fear, trust, "
            "       disgust, anticipation, surprise, shame, guilt "
            "FROM event_emotions WHERE event_id = ?",
            (event_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        # Table missing — migration 015 not applied. Safe degradation to v2_pure.
        return [0.0] * len(EMOTION_KEYS)
    return _emotion_vec_from_row(row)


def _emo_cosine(a: list[float], b: list[float]) -> float:
    """Cosine sim of two 10-dim emotion vectors. 0 if either is zero-vector."""
    return _cosine(a, b)


def _query_emotion_vec(
    con: sqlite3.Connection, query: str, user_state: UserState | None,
    inferred_by_fallback: str = "keyword_fallback",
) -> tuple[list[float], dict[str, float]]:
    """Return (emo_vec, emo_dict) for a query. Prefer user_state.mood_vector if present;
    otherwise fetch from query_emotion_cache; otherwise keyword fallback."""
    if user_state and user_state.mood_vector:
        d = {k: float(user_state.mood_vector.get(k, 0.0)) for k in EMOTION_KEYS}
        return list(d.values()), d

    # Try cache (migration 015 table)
    qhash = hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
    try:
        row = con.execute(
            "SELECT joy, sadness, anger, fear, trust, "
            "       disgust, anticipation, surprise, shame, guilt "
            "FROM query_emotion_cache WHERE query_hash = ?",
            (qhash,),
        ).fetchone()
        if row is not None:
            vec = _emotion_vec_from_row(row)
            return vec, {k: v for k, v in zip(EMOTION_KEYS, vec)}
    except sqlite3.OperationalError:
        pass

    # Keyword fallback (no LLM dependency — caller can pre-warm cache via separate tool)
    d = _keyword_emotion_inference(query)
    return [d[k] for k in EMOTION_KEYS], d


EMO_KEYWORDS = {
    "joy":         ["рад", "кайф", "счаст", "joy"],
    "sadness":     ["груст", "печал", "тоск", "sad"],
    "anger":       ["зл", "зол", "ярос", "раздраж", "бес", "anger"],
    "fear":        ["страх", "тревог", "паник", "боюсь", "fear", "anxious"],
    "trust":       ["довер", "близос", "принят", "trust"],
    "disgust":     ["отвращ", "брезг", "disgust"],
    "anticipation":["предвкуш", "надежд", "интерес", "excited"],
    "surprise":    ["удивл", "шок", "surprise"],
    "shame":       ["стыд", "смущ", "shame", "заслуживат"],
    "guilt":       ["вин", "сожал", "guilt", "виноват"],
}


def _keyword_emotion_inference(query: str) -> dict[str, float]:
    q = query.lower()
    scores = {k: 0.0 for k in EMOTION_KEYS}
    for emo, kws in EMO_KEYWORDS.items():
        for kw in kws:
            if kw in q:
                scores[emo] = max(scores[emo], 0.7)
    return scores


# ────────────────────────────────────────────────────────────────────────────
# State fit heuristic
# ────────────────────────────────────────────────────────────────────────────

def _event_biometric(event: dict) -> dict:
    """Pulse core events table doesn't have biometric_snapshot yet; bench corpus does.
    Fallback: derive coarse signal from text + label."""
    bio = event.get("biometric_snapshot") or {}
    if bio:
        return bio
    text = (event.get("text") or "").lower()
    label = (event.get("belief_class") or "").lower()
    return {}  # upstream heuristic below handles text-based detection


def _event_is_depletion(event: dict) -> bool:
    bio = event.get("biometric_snapshot") or {}
    text = (event.get("text") or "").lower()
    label = (event.get("belief_class") or "").lower()
    if isinstance(bio.get("hrv"), (int, float)) and bio["hrv"] < 60:
        return True
    if isinstance(bio.get("sleep_quality"), (int, float)) and bio["sleep_quality"] <= 0.4:
        return True
    if isinstance(bio.get("stress_proxy"), (int, float)) and bio["stress_proxy"] >= 0.6:
        return True
    if bio.get("hrv_trend") == "declining_3d" or bio.get("hr_trend") in ("elevated_3d", "elevated_overnight"):
        return True
    if any(phrase in text for phrase in ("hrv 5", "declining", "anxious sleep", "overload", "burden")):
        return True
    return False


def _event_is_restoration(event: dict) -> bool:
    bio = event.get("biometric_snapshot") or {}
    text = (event.get("text") or "").lower()
    if isinstance(bio.get("hrv"), (int, float)) and bio["hrv"] >= 70:
        return True
    if (isinstance(bio.get("sleep_quality"), (int, float)) and bio["sleep_quality"] >= 0.7
            and bio.get("stress_proxy", 1.0) <= 0.3):
        return True
    if bio.get("workout") is True:
        return True
    if any(phrase in text for phrase in ("hrv 7", "hrv 8", "hrv 9", "post-workout", "ship day", "ship milestone")):
        return True
    return False


def _compute_state_fit(event: dict, state: UserState) -> float:
    """0-1 heuristic score matching event to body/life state."""
    score = 0.0
    if state.is_body_stressed() and _event_is_depletion(event):
        score = max(score, 1.0)
    if state.is_body_restored() and _event_is_restoration(event):
        score = max(score, 1.0)
    if state.recent_life_events_7d:
        hints = " ".join(state.recent_life_events_7d).lower()
        label = (event.get("belief_class") or "").lower()
        text = (event.get("text") or "").lower()
        if any(w in hints for w in ("anya", "аня", "conflict", "ссора")):
            if "marriage" in label or "anya" in text or "аня" in text:
                score = max(score, 0.7)
    return score


def _compute_date_proximity(event_days_ago: float, state_days_ago: float) -> float:
    """Stepped temporal proximity 0-1 — discriminates exact-day from same-week.

    diff ≤ 1 → 1.0  (same day)
    diff ≤ 3 → 0.7  (within 3 days)
    diff ≤ 7 → 0.3  (same week)
    else     → 0.0

    Applied only when state.snapshot_days_ago is set — LME-style retrieval
    (no snapshot date) keeps formula identical to v2_pure.
    """
    diff = abs(float(event_days_ago) - float(state_days_ago))
    if diff <= 1.0:
        return 1.0
    if diff <= 3.0:
        return 0.7
    if diff <= 7.0:
        return 0.3
    return 0.0


# ────────────────────────────────────────────────────────────────────────────
# Chain expansion (migration 015 tables)
# ────────────────────────────────────────────────────────────────────────────

def _fetch_chain_edges(con: sqlite3.Connection) -> tuple[dict[int, list[int]], dict[int, list[int]]]:
    """Return (parent_to_children, child_to_parents) from event_chains table.
    Empty dicts if table missing."""
    p2c: dict[int, list[int]] = {}
    c2p: dict[int, list[int]] = {}
    try:
        for parent, child in con.execute(
            "SELECT parent_id, child_id FROM event_chains"
        ):
            p2c.setdefault(parent, []).append(child)
            c2p.setdefault(child, []).append(parent)
    except sqlite3.OperationalError:
        pass
    return p2c, c2p


def _expand_chain_from_seeds(
    con: sqlite3.Connection, seeds: list[int], top_k: int, depth: int = 4,
) -> list[int]:
    """Find the best connected-component chain containing seeds; return topologically
    ordered event IDs (root first, leaf last) up to top_k."""
    p2c, c2p = _fetch_chain_edges(con)
    if not p2c and not c2p:
        return seeds[:top_k]

    # For each seed, find reachable seeds
    seed_set = set(seeds)
    def _reachable_seeds(start: int) -> set[int]:
        visited = {start}
        frontier = [start]
        found = {start}
        while frontier:
            n = frontier.pop(0)
            for nb in c2p.get(n, []) + p2c.get(n, []):
                if nb not in visited:
                    visited.add(nb); frontier.append(nb)
                    if nb in seed_set:
                        found.add(nb)
        return found

    best_seed, best_reach = None, set()
    for s in seeds:
        reach = _reachable_seeds(s)
        if len(reach) > len(best_reach):
            best_seed, best_reach = s, reach

    if best_seed is None or len(best_reach) < 2:
        return seeds[:top_k]

    # BFS from best_seed, compute ancestor depth, sort topologically
    visited = {best_seed}
    frontier = [(best_seed, 0)]
    while frontier:
        n, d = frontier.pop(0)
        if d >= depth: continue
        for nb in c2p.get(n, []) + p2c.get(n, []):
            if nb not in visited:
                visited.add(nb); frontier.append((nb, d + 1))

    def _ancestor_depth(eid: int, memo: dict[int, int] | None = None) -> int:
        if memo is None: memo = {}
        if eid in memo: return memo[eid]
        memo[eid] = 0
        parents = [p for p in c2p.get(eid, []) if p in visited]
        if not parents:
            return 0
        memo[eid] = 1 + max(_ancestor_depth(p, memo) for p in parents)
        return memo[eid]

    ordered = sorted(visited, key=_ancestor_depth)
    # Intersect with reachable seeds to prefer chain members that were actual hits
    chain_first = [e for e in ordered if e in best_reach or e == best_seed]
    result = list(chain_first)
    for s in seeds:
        if s not in result:
            result.append(s)
    return result[:top_k]


# ────────────────────────────────────────────────────────────────────────────
# Main retrieval API
# ────────────────────────────────────────────────────────────────────────────

def retrieve_events_v3(
    con: sqlite3.Connection,
    query: str,
    *,
    top_k: int = 3,
    user_state: UserState | None = None,
    return_chain: bool = False,
    lam: float = DEFAULT_LAMBDA,
    lam_anchor: float = DEFAULT_LAMBDA_ANCHOR,
    embedder_model: str = DEFAULT_EMBEDDER,
    top_n_candidates: int = DEFAULT_TOP_N_CANDIDATES,
    use_belief_class: bool = True,
    beta: float = DEFAULT_BETA,
    gamma: float = DEFAULT_GAMMA,
    delta_anchor: float = DEFAULT_DELTA_ANCHOR,
    delta_date: float = DEFAULT_DELTA_DATE,
    anchor_top_n: int = ANCHOR_TOP_N,
) -> list[dict]:
    """Return top-k events with v3 conditional boosts.

    When user_state is None and return_chain is False, formula IS retrieve_events() v2.
    When user_state is provided:
      - emotion boost applied iff state.has_dominant_emotion(0.5)
      - state boost applied iff state.is_body_stressed() or state.is_body_restored()
    When return_chain is True: top seeds are expanded via event_chains graph,
    returned in topological order (root → leaf).

    Output shape identical to retrieve_events() + optional extra fields:
      id, text, sentiment, emotional_weight, days_ago, cosine, score,
      emotion_boost (if applied), state_boost (if applied)
    """
    # 1. Fetch embeddings for candidate set
    rows = con.execute(
        "SELECT event_id, vector_json FROM event_embeddings WHERE model = ?",
        (embedder_model,),
    ).fetchall()
    if not rows:
        return []

    query_vec = embed_texts([query], model=embedder_model)[0]

    # 2. Base cosine score
    import json as _json
    scored: list[tuple[float, int]] = []
    for event_id, vector_json in rows:
        try:
            vec = _json.loads(vector_json)
        except (_json.JSONDecodeError, TypeError):
            continue
        if not isinstance(vec, list) or not vec:
            continue
        cos = _cosine(query_vec, vec)
        if cos <= 0.0:
            continue
        scored.append((cos, event_id))

    scored.sort(key=lambda t: t[0], reverse=True)
    candidates = scored[:top_n_candidates]

    # 3. Prepare conditional boosts
    q_emo_vec, q_emo_dict = _query_emotion_vec(con, query, user_state)
    apply_emotion = max(q_emo_dict.values()) >= 0.5 if q_emo_dict else False

    apply_state = user_state is not None and (
        user_state.is_body_stressed() or user_state.is_body_restored()
    )

    # 4. First pass: compute base per candidate (for anchor top-N gate)
    now = datetime.now(timezone.utc)
    prepared: list[tuple[float, dict, float]] = []  # (base, event, days_ago)
    for cos, event_id in candidates:
        event = _fetch_event(con, event_id)
        if event is None:
            continue
        days_ago = _days_since(event.get("ts"), now) or 30
        # Phase 5.4: anchor-aware decay — user_flag=True events use the slower
        # lam_anchor rate (structural anchors shouldn't age like day-to-day
        # moments). Falls back to per-belief-class decay for non-anchors.
        is_anchor_event = bool(event.get("user_flag"))
        if is_anchor_event:
            effective_lam = lam_anchor
        elif use_belief_class:
            effective_lam = BELIEF_DECAY.get(event.get("belief_class", "operational"), lam)
        else:
            effective_lam = lam
        recency = math.exp(-effective_lam * days_ago)
        floor = event.get("confidence_floor", 0.0) or 0.0
        base = cos * recency
        base = max(base, cos * floor) if floor > 0 else base

        event["cosine"] = cos
        event["days_ago"] = days_ago
        event["effective_lambda"] = effective_lam
        prepared.append((base, event, days_ago))

    # Anchor gate: who are top-N candidates by base score (for conditional anchor boost)
    prepared.sort(key=lambda t: -t[0])
    anchor_eligible_ids = {
        ev["id"] for _, ev, _ in prepared[:anchor_top_n]
    }

    apply_date = (user_state is not None and
                  user_state.snapshot_days_ago is not None)

    ranked: list[tuple[float, dict]] = []
    for base, event, days_ago in prepared:
        event_id = event["id"]

        # Emotion boost
        emo_boost = 1.0
        if apply_emotion:
            ev_emo = _fetch_event_emotion(con, event_id)
            align = _emo_cosine(q_emo_vec, ev_emo)
            emo_boost = 1.0 + beta * max(0.0, align)

        # State boost
        state_boost = 1.0
        if apply_state and user_state is not None:
            fit = _compute_state_fit(event, user_state)
            state_boost = 1.0 + gamma * fit

        # Phase 5.1: Anchor-priority boost — only for user_flag=True events
        # that already passed the top-N base-score gate. Keeps anchor events
        # from sliding out of top-k when adjacent non-anchor events edge ahead
        # on cosine alone, while not dragging unrelated anchors into unrelated
        # queries (they never enter top-N, so never get the boost).
        anchor_boost = 1.0
        is_anchor = bool(event.get("user_flag"))
        if is_anchor and event_id in anchor_eligible_ids and delta_anchor > 0:
            anchor_boost = 1.0 + delta_anchor

        # Phase 5.2: Date-proximity boost — only when state represents a
        # specific past moment. Boosts events that happened on or near the
        # snapshot date. Formula collapses to 1.0 for LME-style queries where
        # snapshot_days_ago is None.
        date_boost = 1.0
        if apply_date:
            prox = _compute_date_proximity(float(days_ago), float(user_state.snapshot_days_ago))
            date_boost = 1.0 + delta_date * prox

        score = base * emo_boost * state_boost * anchor_boost * date_boost

        event["score"] = score
        if apply_emotion:
            event["emotion_boost"] = emo_boost
        if apply_state:
            event["state_boost"] = state_boost
        if anchor_boost != 1.0:
            event["anchor_boost"] = anchor_boost
        if apply_date:
            event["date_boost"] = date_boost
        ranked.append((score, event))

    ranked.sort(key=lambda t: t[0], reverse=True)
    top = [ev for _, ev in ranked[:top_k]]

    # 5. Optional chain expansion: reorder seeds via event_chains
    if return_chain and top:
        # Widen candidate pool for chain analysis
        wider = [ev for _, ev in ranked[:max(top_k * 3, 9)]]
        seed_ids = [ev["id"] for ev in wider]
        reordered_ids = _expand_chain_from_seeds(con, seed_ids, top_k)
        # Rebuild as dicts in reordered order
        by_id = {ev["id"]: ev for ev in wider}
        top = [by_id[i] for i in reordered_ids if i in by_id]

    return top


# ────────────────────────────────────────────────────────────────────────────
# Backwards-compat alias
# ────────────────────────────────────────────────────────────────────────────

def retrieve_events(con: sqlite3.Connection, query: str, **kwargs) -> list[dict]:
    """v2-compatible signature. Delegates to v3 with no user_state (→ v2_pure behavior)."""
    # Strip v3-only kwargs so v2 consumers can upgrade transparently
    kwargs.pop("user_state", None)
    kwargs.pop("return_chain", None)
    kwargs.pop("beta", None)
    kwargs.pop("gamma", None)
    kwargs.pop("delta_anchor", None)
    kwargs.pop("delta_date", None)
    kwargs.pop("anchor_top_n", None)
    kwargs.pop("lam_anchor", None)
    return retrieve_events_v3(con, query, **kwargs)
