"""Keyword-based graph retrieval for Phase 2.

Tokenize user message → match entities by name/alias → BFS expansion up to depth hops → rank.
"""

import json
import re
import sqlite3
from collections import deque
from datetime import datetime, timezone


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
        "SELECT id, canonical_name, kind, aliases, salience_score, last_seen FROM entities"
    ).fetchall()

    for row in all_entities:
        eid, name, kind, aliases_json, salience, last_seen = row
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
                        "last_seen": last_seen,
                    }
                break

    return list(matched.values())


def _get_entity_full(
    con: sqlite3.Connection, entity_id: int
) -> dict | None:
    """Fetch a single entity by ID for BFS-discovered entities."""
    row = con.execute(
        "SELECT id, canonical_name, kind, aliases, salience_score, last_seen FROM entities WHERE id = ?",
        (entity_id,),
    ).fetchone()
    if row is None:
        return None
    eid, name, kind, aliases_json, salience, last_seen = row
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
    now = datetime.now(timezone.utc)
    scored = []
    for ent in entities:
        try:
            last = datetime.fromisoformat(ent["last_seen"].replace("Z", "+00:00"))
            days_ago = (now - last).days
        except (ValueError, AttributeError):
            days_ago = 365
        recency = max(0.1, 1.0 - days_ago / 365)
        hop = ent.get("_hop", 0)
        hop_penalty = 0.7 ** hop
        scored.append((ent["salience_score"] * recency * hop_penalty, ent))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [ent for _, ent in scored]
