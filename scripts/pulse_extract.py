#!/usr/bin/env python3
"""pulse_extract — run one iteration of the two-pass extractor loop.

Reads pending extraction_jobs, runs Sonnet triage + Opus extract, writes
entities/relations/events/facts/evidence, advances job state.
"""

import anthropic
import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from extract import prompts, resolver, scorer

_client_cache = None

TRIAGE_MODEL = "claude-sonnet-4-6"
EXTRACT_MODEL = "claude-opus-4-6"


def _anthropic_client():
    """Lazy, cached Anthropic client. Raises RuntimeError if key missing."""
    global _client_cache
    if _client_cache is None:
        key = os.getenv("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY env var required for extraction")
        _client_cache = anthropic.Anthropic(api_key=key)
    return _client_cache


def _open_connection(db_path: str) -> sqlite3.Connection:
    """Open a sqlite3 connection wired for the extractor.

    - PRAGMA busy_timeout=5000: survive WAL contention with the Go ingest process
      (the Go side sets the same value via DSN in internal/store/store.go).
    - PRAGMA foreign_keys=ON: schema assumes FK enforcement.
    - isolation_level=None: manual BEGIN/COMMIT so we can scope transactions to a
      single observation and use SAVEPOINT per item.
    """
    con = sqlite3.connect(db_path)
    con.isolation_level = None
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=5000")
    return con


def call_sonnet_triage(prompt: str, expected_count: int) -> list[dict]:
    client = _anthropic_client()
    msg = client.messages.create(
        model=TRIAGE_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(block.text for block in msg.content if hasattr(block, "text"))
    return prompts.parse_triage_response(text, expected_count)


def call_opus_extract(prompt: str) -> dict:
    client = _anthropic_client()
    msg = client.messages.create(
        model=EXTRACT_MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(block.text for block in msg.content if hasattr(block, "text"))
    return prompts.parse_extract_response(text)


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


def _apply_extraction(con: sqlite3.Connection, obs_id: int, result: dict) -> dict:
    """Apply one extraction result to the graph. Caller owns the outer transaction.

    Each item (entity/event/relation/fact) is wrapped in SAVEPOINT so an
    sqlite3.IntegrityError on one item does not abort the others. The caller's
    outer tx stays open on return.
    """
    report = {
        "obs_id": obs_id,
        "entities_written": 0,
        "events_written": 0,
        "relations_written": 0,
        "facts_written": 0,
        "failed_items": [],
    }

    existing = _load_existing_entities(con)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    name_to_id: dict[str, int] = {}

    def _item_failure(item_kind: str, reason: str, detail: dict) -> None:
        report["failed_items"].append({"item_kind": item_kind, "reason": reason, "detail": detail})

    # --- entities ---
    for idx, ent in enumerate(result.get("entities", [])):
        sp = f"ent_{idx}"
        con.execute(f"SAVEPOINT {sp}")
        try:
            dec = resolver.resolve_entity(ent, existing)
            scored = scorer.score_entity(ent)
            if dec.action == "bind_identity":
                con.execute(
                    "UPDATE entities SET last_seen=?, salience_score=?, emotional_weight=?, scorer_version=? WHERE id=?",
                    (now, scored["salience_score"], scored["emotional_weight"], scored["scorer_version"], dec.entity_id),
                )
                entity_id = dec.entity_id
            else:
                cur = con.execute(
                    "INSERT INTO entities (canonical_name, kind, aliases, first_seen, last_seen, salience_score, emotional_weight, scorer_version) VALUES (?,?,?,?,?,?,?,?)",
                    (ent["canonical_name"], ent.get("kind", "person"), json.dumps(ent.get("aliases") or []),
                     now, now, scored["salience_score"], scored["emotional_weight"], scored["scorer_version"]),
                )
                entity_id = cur.lastrowid
                existing.append({"id": entity_id, "canonical_name": ent["canonical_name"], "kind": ent.get("kind", "person"), "aliases": ent.get("aliases") or []})

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

            con.execute(
                "INSERT INTO evidence (subject_kind, subject_id, observation_id, created_at) VALUES ('entity',?,?,?)",
                (entity_id, obs_id, now),
            )
            con.execute(f"RELEASE SAVEPOINT {sp}")

            name_to_id[ent["canonical_name"]] = entity_id
            for alias in (ent.get("aliases") or []):
                name_to_id[alias] = entity_id
            report["entities_written"] += 1
        except sqlite3.Error as ex:
            con.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            con.execute(f"RELEASE SAVEPOINT {sp}")
            _item_failure("entity", str(ex)[:200], {"index": idx, "canonical_name": ent.get("canonical_name", ""), "kind": ent.get("kind", "")})

    # --- events ---
    for idx, ev in enumerate(result.get("events", [])):
        involved = ev.get("entities_involved") or []
        if not involved:
            _item_failure("event", "orphan_event_no_entities_involved", {"title": ev.get("title", "")})
            continue
        sp = f"ev_{idx}"
        con.execute(f"SAVEPOINT {sp}")
        try:
            s = scorer.score_event(ev)
            cur = con.execute(
                "INSERT INTO events (title, description, sentiment, emotional_weight, scorer_version, ts) VALUES (?,?,?,?,?,?)",
                (ev.get("title", ""), ev.get("description", ""), s["sentiment"], s["emotional_weight"], s["scorer_version"], ev.get("ts", now)),
            )
            con.execute(
                "INSERT INTO evidence (subject_kind, subject_id, observation_id, created_at) VALUES ('event',?,?,?)",
                (cur.lastrowid, obs_id, now),
            )
            con.execute(f"RELEASE SAVEPOINT {sp}")
            report["events_written"] += 1
        except sqlite3.Error as ex:
            con.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            con.execute(f"RELEASE SAVEPOINT {sp}")
            _item_failure("event", str(ex)[:200], {"index": idx, "title": ev.get("title", "")})

    # --- relations ---
    for idx, rel in enumerate(result.get("relations", [])):
        from_id = name_to_id.get(rel.get("from", ""))
        to_id = name_to_id.get(rel.get("to", ""))
        if from_id is None or to_id is None:
            _item_failure("relation", "unknown_entity", {"from": rel.get("from", ""), "to": rel.get("to", "")})
            continue
        sp = f"rel_{idx}"
        con.execute(f"SAVEPOINT {sp}")
        try:
            cur = con.execute(
                "INSERT INTO relations (from_entity_id, to_entity_id, kind, strength, first_seen, last_seen) VALUES (?,?,?,?,?,?)",
                (from_id, to_id, rel.get("kind", ""), float(rel.get("strength", 0.0)), now, now),
            )
            con.execute(
                "INSERT INTO evidence (subject_kind, subject_id, observation_id, created_at) VALUES ('relation',?,?,?)",
                (cur.lastrowid, obs_id, now),
            )
            con.execute(f"RELEASE SAVEPOINT {sp}")
            report["relations_written"] += 1
        except sqlite3.Error as ex:
            con.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            con.execute(f"RELEASE SAVEPOINT {sp}")
            _item_failure("relation", str(ex)[:200], {"index": idx, "from": rel.get("from", ""), "to": rel.get("to", ""), "kind": rel.get("kind", "")})

    # --- facts ---
    for idx, fact in enumerate(result.get("facts", [])):
        entity_id = name_to_id.get(fact.get("entity", ""))
        if entity_id is None:
            _item_failure("fact", "unknown_entity", {"entity": fact.get("entity", ""), "text": fact.get("text", "")[:80]})
            continue
        sp = f"fact_{idx}"
        con.execute(f"SAVEPOINT {sp}")
        try:
            scored = scorer.score_fact(fact)
            cur = con.execute(
                "INSERT INTO facts (entity_id, text, confidence, scorer_version, created_at) VALUES (?,?,?,?,?)",
                (entity_id, fact.get("text", ""), scored["confidence"], scored["scorer_version"], now),
            )
            con.execute(
                "INSERT INTO evidence (subject_kind, subject_id, observation_id, created_at) VALUES ('fact',?,?,?)",
                (cur.lastrowid, obs_id, now),
            )
            con.execute(f"RELEASE SAVEPOINT {sp}")
            report["facts_written"] += 1
        except sqlite3.Error as ex:
            con.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            con.execute(f"RELEASE SAVEPOINT {sp}")
            _item_failure("fact", str(ex)[:200], {"index": idx, "entity": fact.get("entity", ""), "text": fact.get("text", "")[:80]})

    return report


def _set_job_state(con: sqlite3.Connection, job_id: int, state: str, *,
                   last_error: str | None = None, increment_attempts: bool = False,
                   triage_model: str | None = None, extract_model: str | None = None) -> None:
    """Update extraction_jobs state in its own committed tx.

    Called at two boundaries: claim (pending -> running, +1 attempt) and
    finalize (running -> done/failed/dlq). Keeps state transitions durable
    regardless of apply-stage success.
    """
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    sets = ["state=?", "updated_at=?"]
    args: list = [state, now]
    if increment_attempts:
        sets.append("attempts=attempts+1")
    if last_error is not None:
        sets.append("last_error=?")
        args.append(last_error[:500])
    if triage_model is not None:
        sets.append("triage_model=?")
        args.append(triage_model)
    if extract_model is not None:
        sets.append("extract_model=?")
        args.append(extract_model)
    args.append(job_id)

    con.execute("BEGIN IMMEDIATE")
    con.execute(f"UPDATE extraction_jobs SET {', '.join(sets)} WHERE id=?", args)
    con.execute("COMMIT")


def run_once(db_path: str, budget_usd_remaining: float = 10.0) -> int:
    con = _open_connection(db_path)

    try:
        if budget_usd_remaining <= 0:
            print("budget exhausted for today — skipping extraction run")
            return 0

        jobs = con.execute(
            "SELECT id, observation_ids FROM extraction_jobs "
            "WHERE state='pending' ORDER BY created_at LIMIT 10"
        ).fetchall()
        if not jobs:
            print("no pending jobs")
            return 0

        for job_id, obs_ids_json in jobs:
            obs_ids = json.loads(obs_ids_json)
            _set_job_state(con, job_id, "running", increment_attempts=True)

            observations = _load_observations(con, obs_ids)
            if not observations:
                _set_job_state(con, job_id, "failed", last_error="no observations")
                continue

            try:
                triage_prompt = prompts.build_triage_prompt(observations)
                verdicts = call_sonnet_triage(triage_prompt, expected_count=len(observations))
                job_reports: list[dict] = []

                for obs, v in zip(observations, verdicts):
                    if v["verdict"] != "extract":
                        continue
                    graph_ctx = {"existing_entities": _load_existing_entities(con)}
                    extract_prompt = prompts.build_extract_prompt(obs, graph_ctx)
                    result = call_opus_extract(extract_prompt)

                    con.execute("BEGIN IMMEDIATE")
                    try:
                        obs_report = _apply_extraction(con, obs["id"], result)
                        con.execute("COMMIT")
                        job_reports.append(obs_report)
                    except Exception as ex:
                        con.execute("ROLLBACK")
                        job_reports.append({
                            "obs_id": obs["id"],
                            "entities_written": 0, "events_written": 0,
                            "relations_written": 0, "facts_written": 0,
                            "failed_items": [{
                                "item_kind": "whole_obs",
                                "reason": f"{type(ex).__name__}: {str(ex)[:200]}",
                                "detail": {},
                            }],
                        })
                        raise

                _set_job_state(
                    con, job_id, "done",
                    triage_model="sonnet-4.6", extract_model="opus-4.6",
                )
                print(f"job {job_id}: done, apply_report={json.dumps(job_reports, ensure_ascii=False)[:500]}")
            except Exception as e:
                attempts_row = con.execute(
                    "SELECT attempts FROM extraction_jobs WHERE id=?", (job_id,)
                ).fetchone()
                attempts = attempts_row[0] if attempts_row else 0
                next_state = "dlq" if attempts >= 3 else "pending"
                _set_job_state(con, job_id, next_state, last_error=str(e))
    finally:
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
