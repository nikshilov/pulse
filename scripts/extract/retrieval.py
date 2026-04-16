"""Keyword-based graph retrieval for Phase 2.

Tokenize user message → match entities by name/alias → BFS expansion up to depth hops → rank.

Ranking is Garden-style (bench winner, 9-way empathic eval, Apr 2026):
    score = (salience + emotional_weight) × recency × anchor × hop_penalty

where:
    - recency = exp(-λ × days_since_last_seen), λ depends on entity kind
      (person decays slowest, concept fastest — matches human forgetting curves)
    - anchor = 1.5 for persons with emotional_weight > 0.6 (Anna/Sonya/Kristina class),
      1.0 otherwise — lifts emotionally central people even when salience is modest
    - emotional_weight is additive (not multiplicative) so salience=0.3 emo=0.9
      does not lose to salience=0.9 emo=0.0
"""

import json
import math
import re
import sqlite3
from collections import deque
from datetime import datetime, timezone


# Retrieval-time exponential decay rates by entity kind (half-lives in days):
#   person  λ=0.001  → t½ ≈ 693d (people stay relevant for years)
#   place   λ=0.003  → t½ ≈ 231d
#   project λ=0.005  → t½ ≈ 139d (projects rotate in/out)
#   concept λ=0.01   → t½ ≈  69d (abstract concepts decay fastest)
#   default λ=0.005
#
# These MUST match scripts/pulse_consolidate.py:DECAY_RATES. Duplicated here to avoid
# a cross-module import between scripts/ and scripts/extract/. If you change one,
# change the other — or factor into a shared module.
DECAY_RATES = {
    "person": 0.001,
    "project": 0.005,
    "place": 0.003,
    "concept": 0.01,
    "default": 0.005,
}


def retrieve_context(
    con: sqlite3.Connection, message: str, top_k: int = 10, depth: int = 1
) -> dict:
    tokens = _tokenize(message)
    seed_entities = _match_entities(con, tokens)
    seed_ids = {ent["id"] for ent in seed_entities}

    # Mark seed entities as hop 0
    for ent in seed_entities:
        ent["_hop"] = 0
        ent["relations"] = _get_relations(con, ent["id"])
        ent["facts"] = _get_facts(con, ent["id"])

    all_entities: dict[int, dict] = {ent["id"]: ent for ent in seed_entities}
    max_depth_used = 0

    if depth > 0:
        # BFS expansion
        frontier: deque[tuple[int, int]] = deque(
            (ent["id"], 0) for ent in seed_entities
        )
        visited: set[int] = set(seed_ids)

        while frontier:
            entity_id, hop = frontier.popleft()
            if hop >= depth:
                continue

            neighbors = _get_neighbor_ids(con, entity_id)
            for neighbor_id in neighbors:
                if neighbor_id in visited:
                    continue
                visited.add(neighbor_id)

                neighbor = _get_entity_full(con, neighbor_id)
                if neighbor is None:
                    continue

                neighbor_hop = hop + 1
                neighbor["_hop"] = neighbor_hop
                neighbor["relations"] = _get_relations(con, neighbor_id)
                neighbor["facts"] = _get_facts(con, neighbor_id)
                all_entities[neighbor_id] = neighbor

                if neighbor_hop > max_depth_used:
                    max_depth_used = neighbor_hop

                frontier.append((neighbor_id, neighbor_hop))

    ranked = _rank(list(all_entities.values()))
    trimmed = ranked[:top_k]

    return {
        "matched_entities": trimmed,
        "total_matched": len(all_entities),
        "retrieval_method": "keyword",
        "max_depth_used": max_depth_used,
    }


def _tokenize(message: str) -> list[str]:
    words = re.findall(r"\b[\w\-]{2,}\b", message, re.UNICODE)
    ngrams = []
    for i in range(len(words)):
        for j in range(i + 1, min(i + 4, len(words) + 1)):
            ngrams.append(" ".join(words[i:j]))
    return list(set(words + ngrams))


def _match_entities(con: sqlite3.Connection, tokens: list[str]) -> list[dict]:
    matched: dict[int, dict] = {}
    all_entities = con.execute(
        "SELECT id, canonical_name, kind, aliases, salience_score, emotional_weight, last_seen FROM entities"
    ).fetchall()

    for row in all_entities:
        eid, name, kind, aliases_json, salience, emo, last_seen = row
        try:
            aliases = json.loads(aliases_json) if aliases_json else []
        except (json.JSONDecodeError, TypeError):
            aliases = []
        all_names = [name] + aliases

        for token in tokens:
            if any(token.lower() == n.lower() for n in all_names):
                if eid not in matched:
                    matched[eid] = {
                        "id": eid,
                        "canonical_name": name,
                        "kind": kind,
                        "aliases": aliases,
                        "salience_score": salience or 0.0,
                        "emotional_weight": emo or 0.0,
                        "last_seen": last_seen,
                    }
                break

    return list(matched.values())


def _get_entity_full(
    con: sqlite3.Connection, entity_id: int
) -> dict | None:
    """Fetch a single entity by ID for BFS-discovered entities."""
    row = con.execute(
        "SELECT id, canonical_name, kind, aliases, salience_score, emotional_weight, last_seen "
        "FROM entities WHERE id = ?",
        (entity_id,),
    ).fetchone()
    if row is None:
        return None
    eid, name, kind, aliases_json, salience, emo, last_seen = row
    try:
        aliases = json.loads(aliases_json) if aliases_json else []
    except (json.JSONDecodeError, TypeError):
        aliases = []
    return {
        "id": eid,
        "canonical_name": name,
        "kind": kind,
        "aliases": aliases,
        "salience_score": salience or 0.0,
        "emotional_weight": emo or 0.0,
        "last_seen": last_seen,
    }


def _get_neighbor_ids(con: sqlite3.Connection, entity_id: int) -> list[int]:
    """Return IDs of entities connected via strong-enough relations (strength > 0.3, limit 5)."""
    rows = con.execute(
        "SELECT CASE WHEN from_entity_id = ? THEN to_entity_id ELSE from_entity_id END "
        "FROM relations "
        "WHERE (from_entity_id = ? OR to_entity_id = ?) AND strength > 0.3 "
        "ORDER BY strength DESC LIMIT 5",
        (entity_id, entity_id, entity_id),
    ).fetchall()
    return [r[0] for r in rows]


def _get_relations(con: sqlite3.Connection, entity_id: int) -> list[dict]:
    rows = con.execute(
        "SELECT r.kind, r.context, r.strength, "
        "CASE WHEN r.from_entity_id = ? THEN e2.canonical_name ELSE e1.canonical_name END "
        "FROM relations r "
        "JOIN entities e1 ON r.from_entity_id = e1.id "
        "JOIN entities e2 ON r.to_entity_id = e2.id "
        "WHERE r.from_entity_id = ? OR r.to_entity_id = ?",
        (entity_id, entity_id, entity_id),
    ).fetchall()
    return [
        {"kind": r[0], "context": r[1] or "", "strength": r[2], "other_entity": r[3]}
        for r in rows
    ]


def _get_facts(con: sqlite3.Connection, entity_id: int) -> list[dict]:
    rows = con.execute(
        "SELECT text, confidence, verified FROM facts WHERE entity_id = ?",
        (entity_id,),
    ).fetchall()
    return [{"text": r[0], "confidence": r[1], "verified": bool(r[2])} for r in rows]


def _rank(entities: list[dict]) -> list[dict]:
    """Rank entities by Garden-style empathic formula.

        score = (salience + emotional_weight) × recency × anchor × hop_penalty

    - recency       exp(-λ × days), λ = DECAY_RATES[kind] — retrieval-time decay
                    (non-destructive, unlike mutation-based decay in consolidation)
    - anchor        1.5 if person with emotional_weight > 0.6 (core people boost)
    - emotional_weight is ADDITIVE so an emo-heavy low-salience memory is not
      crushed by a salience-heavy but emotionally flat one
    - hop_penalty   0.7 ^ hop (direct match > 1-hop > 2-hop)
    """
    now = datetime.now(timezone.utc)
    scored = []
    for ent in entities:
        try:
            last = datetime.fromisoformat(ent["last_seen"].replace("Z", "+00:00"))
            days_ago = max(0, (now - last).days)
        except (ValueError, AttributeError, TypeError):
            days_ago = 365

        kind = ent.get("kind") or "default"
        lam = DECAY_RATES.get(kind, DECAY_RATES["default"])
        recency = math.exp(-lam * days_ago)

        salience = float(ent.get("salience_score") or 0.0)
        emo = float(ent.get("emotional_weight") or 0.0)
        anchor = 1.5 if kind == "person" and emo > 0.6 else 1.0

        hop = ent.get("_hop", 0)
        hop_penalty = 0.7 ** hop

        score = (salience + emo) * recency * anchor * hop_penalty
        scored.append((score, ent))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [ent for _, ent in scored]
