#!/usr/bin/env python3
"""pulse_extract — run one iteration of the two-pass extractor loop.

Reads pending extraction_jobs, runs Sonnet triage + Opus extract, writes
entities/relations/events/facts/evidence, advances job state.
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from extract import prompts, resolver, scorer

# These wrap anthropic SDK in real code. For tests they're monkey-patched.
def call_sonnet_triage(prompt: str) -> list[dict]:
    raise NotImplementedError("wire up anthropic client before production run")


def call_opus_extract(prompt: str) -> dict:
    raise NotImplementedError("wire up anthropic client before production run")


def _load_observations(con: sqlite3.Connection, ids: list[int]) -> list[dict]:
    q = "SELECT id, source_kind, content_text, actors, metadata FROM observations WHERE id IN (%s)" % ",".join("?" * len(ids))
    out = []
    for row in con.execute(q, ids):
        out.append({
            "id": row[0],
            "source_kind": row[1],
            "content_text": row[2],
            "actors": json.loads(row[3] or "[]"),
            "metadata": json.loads(row[4] or "{}"),
        })
    return out


def _load_existing_entities(con: sqlite3.Connection) -> list[dict]:
    rows = con.execute("SELECT id, canonical_name, kind, aliases FROM entities").fetchall()
    return [
        {"id": r[0], "canonical_name": r[1], "kind": r[2], "aliases": json.loads(r[3] or "[]")}
        for r in rows
    ]


def _apply_extraction(con: sqlite3.Connection, obs_id: int, result: dict) -> None:
    existing = _load_existing_entities(con)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # name_to_id: canonical_name + all aliases → entity_id
    name_to_id: dict[str, int] = {}

    for ent in result.get("entities", []):
        dec = resolver.resolve_entity(ent, existing)
        scored = scorer.score_entity(ent)
        if dec.action == "bind_identity":
            # update last_seen + scores (we trust new data equally)
            con.execute("UPDATE entities SET last_seen=?, salience_score=?, emotional_weight=?, scorer_version=? WHERE id=?",
                        (now, scored["salience_score"], scored["emotional_weight"], scored["scorer_version"], dec.entity_id))
            entity_id = dec.entity_id
        else:
            cur = con.execute(
                "INSERT INTO entities (canonical_name, kind, aliases, first_seen, last_seen, salience_score, emotional_weight, scorer_version) VALUES (?,?,?,?,?,?,?,?)",
                (ent["canonical_name"], ent.get("kind", "person"), json.dumps(ent.get("aliases") or []),
                 now, now, scored["salience_score"], scored["emotional_weight"], scored["scorer_version"]),
            )
            entity_id = cur.lastrowid
            existing.append({"id": entity_id, "canonical_name": ent["canonical_name"], "kind": ent.get("kind","person"), "aliases": ent.get("aliases") or []})

            if dec.action == "proposal" and dec.entity_id:
                con.execute(
                    "INSERT INTO entity_merge_proposals (from_entity_id, to_entity_id, confidence, evidence_md, state, proposed_at) VALUES (?,?,?,?,?,?)",
                    (entity_id, dec.entity_id, dec.confidence, dec.reason, "pending", now),
                )
            elif dec.action == "new_entity_with_question":
                ttl = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 7 * 86400))
                con.execute(
                    "INSERT INTO open_questions (subject_entity_id, question_text, asked_at, ttl_expires_at, state) VALUES (?,?,?,?,?)",
                    (entity_id, f"Is {ent['canonical_name']} a new person, or an alias of someone I know?", now, ttl, "open"),
                )

        con.execute("INSERT INTO evidence (subject_kind, subject_id, observation_id, created_at) VALUES ('entity',?,?,?)",
                    (entity_id, obs_id, now))

        # Register canonical name and all aliases in the lookup map
        name_to_id[ent["canonical_name"]] = entity_id
        for alias in (ent.get("aliases") or []):
            name_to_id[alias] = entity_id

    # events
    for ev in result.get("events", []):
        s = scorer.score_event(ev)
        cur = con.execute("INSERT INTO events (title, description, sentiment, emotional_weight, scorer_version, ts) VALUES (?,?,?,?,?,?)",
                          (ev.get("title",""), ev.get("description",""), s["sentiment"], s["emotional_weight"], s["scorer_version"], ev.get("ts", now)))
        con.execute("INSERT INTO evidence (subject_kind, subject_id, observation_id, created_at) VALUES ('event',?,?,?)",
                    (cur.lastrowid, obs_id, now))

    # relations
    for rel in result.get("relations", []):
        from_name = rel.get("from", "")
        to_name = rel.get("to", "")
        from_id = name_to_id.get(from_name)
        to_id = name_to_id.get(to_name)
        if from_id is None:
            print(f"skipping relation: unknown entity '{from_name}'")
            continue
        if to_id is None:
            print(f"skipping relation: unknown entity '{to_name}'")
            continue
        cur = con.execute(
            "INSERT INTO relations (from_entity_id, to_entity_id, kind, strength, first_seen, last_seen) VALUES (?,?,?,?,?,?)",
            (from_id, to_id, rel.get("kind", ""), float(rel.get("strength", 0.0)), now, now),
        )
        con.execute("INSERT INTO evidence (subject_kind, subject_id, observation_id, created_at) VALUES ('relation',?,?,?)",
                    (cur.lastrowid, obs_id, now))

    # facts
    for fact in result.get("facts", []):
        entity_name = fact.get("entity", "")
        entity_id = name_to_id.get(entity_name)
        if entity_id is None:
            print(f"skipping fact: unknown entity '{entity_name}'")
            continue
        scored = scorer.score_fact(fact)
        cur = con.execute(
            "INSERT INTO facts (entity_id, text, confidence, scorer_version, created_at) VALUES (?,?,?,?,?)",
            (entity_id, fact.get("text", ""), scored["confidence"], scored["scorer_version"], now),
        )
        con.execute("INSERT INTO evidence (subject_kind, subject_id, observation_id, created_at) VALUES ('fact',?,?,?)",
                    (cur.lastrowid, obs_id, now))


def run_once(db_path: str, budget_usd_remaining: float = 10.0) -> int:
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys=ON")

    if budget_usd_remaining <= 0:
        print("budget exhausted for today — skipping extraction run")
        con.close()
        return 0

    jobs = con.execute("SELECT id, observation_ids FROM extraction_jobs WHERE state='pending' ORDER BY created_at LIMIT 10").fetchall()
    if not jobs:
        print("no pending jobs")
        return 0

    for job_id, obs_ids_json in jobs:
        obs_ids = json.loads(obs_ids_json)
        con.execute("UPDATE extraction_jobs SET state='running', attempts=attempts+1, updated_at=? WHERE id=?",
                    (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), job_id))
        con.commit()

        observations = _load_observations(con, obs_ids)
        if not observations:
            con.execute("UPDATE extraction_jobs SET state='failed', last_error='no observations', updated_at=? WHERE id=?",
                        (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), job_id))
            con.commit()
            continue

        try:
            triage_prompt = prompts.build_triage_prompt(observations)
            verdicts = call_sonnet_triage(triage_prompt)

            for obs, v in zip(observations, verdicts):
                if v["verdict"] != "extract":
                    continue
                graph_ctx = {"existing_entities": _load_existing_entities(con)}
                extract_prompt = prompts.build_extract_prompt(obs, graph_ctx)
                result = call_opus_extract(extract_prompt)
                _apply_extraction(con, obs["id"], result)

            con.execute("UPDATE extraction_jobs SET state='done', triage_model='sonnet-4.6', extract_model='opus-4.6', updated_at=? WHERE id=?",
                        (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), job_id))
            con.commit()
        except Exception as e:
            attempts_row = con.execute("SELECT attempts FROM extraction_jobs WHERE id=?", (job_id,)).fetchone()
            attempts = attempts_row[0] if attempts_row else 0
            next_state = "dlq" if attempts >= 3 else "pending"  # pending → retry on next run
            con.execute("UPDATE extraction_jobs SET state=?, last_error=?, updated_at=? WHERE id=?",
                        (next_state, str(e)[:500], time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), job_id))
            con.commit()

    con.close()
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", required=True)
    p.add_argument("--budget", type=float, default=float(os.getenv("PULSE_DAILY_EXTRACT_BUDGET_USD", "10")))
    args = p.parse_args()
    return run_once(args.db, args.budget)


if __name__ == "__main__":
    sys.exit(main())
