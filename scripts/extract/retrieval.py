"""Keyword-based graph retrieval for Phase 2.

Tokenize user message → match entities by name/alias → 1-hop expansion → rank.
"""

import json
import re
import sqlite3
from datetime import datetime, timezone


def retrieve_context(
    con: sqlite3.Connection, message: str, top_k: int = 10
) -> dict:
    tokens = _tokenize(message)
    matched = _match_entities(con, tokens)

    for ent in matched:
        ent["relations"] = _get_relations(con, ent["id"])
        ent["facts"] = _get_facts(con, ent["id"])

    ranked = _rank(matched)
    trimmed = ranked[:top_k]

    return {
        "matched_entities": trimmed,
        "total_matched": len(matched),
        "retrieval_method": "keyword",
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
        aliases = json.loads(aliases_json) if aliases_json else []
        all_names = [name] + aliases

        for token in tokens:
            if any(token.lower() == n.lower() for n in all_names):
                if eid not in matched:
                    matched[eid] = {
                        "id": eid,
                        "canonical_name": name,
                        "kind": kind,
                        "aliases": aliases,
                        "salience_score": salience,
                        "last_seen": last_seen,
                    }
                break

    return list(matched.values())


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
        scored.append((ent["salience_score"] * recency, ent))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [ent for _, ent in scored]
