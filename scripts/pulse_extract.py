#!/usr/bin/env python3
"""pulse_extract — run one iteration of the two-pass extractor loop.

Reads pending extraction_jobs, runs Sonnet triage + Opus extract, writes
entities/relations/events/facts/evidence, advances job state.

Cost controls (2026-04, FinOps pass):
- Prompt caching via `cache_control: {type: "ephemeral"}` on the static prefix
  of both triage and extract prompts, and on the tool definition.
- Top-K candidate entities (not full table) shipped into the Opus prompt.
- Live budget gate: reads today's spend from `extraction_metrics.cost_usd`
  before every Anthropic call and aborts mid-batch if we cross the budget.
- Per-call `cost_usd` computed from token usage + model pricing and persisted
  to `extraction_metrics` on every insert.
"""

import anthropic
import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from extract import prompts, resolver, scorer
from extract import tool_schemas

_client_cache = None

TRIAGE_MODEL = "claude-sonnet-4-6"
EXTRACT_MODEL = "claude-opus-4-6"

# Conservative worst-case estimate for "one more Opus extract call" used by the
# pre-flight budget gate. Calibrated at ~800 output tokens + typical cached
# input. If real spend consistently lands much lower, tune this down.
WORST_CASE_NEXT_JOB_USD = 0.10

# Anthropic pricing as of 2026-04 (per 1M tokens, USD). Cache write = 1.25×
# input, cache read = 0.1× input.
PRICING = {
    "claude-sonnet-4-6": {
        "input_per_1m": 3.00,
        "output_per_1m": 15.00,
        "cache_write_mult": 1.25,
        "cache_read_mult": 0.10,
    },
    "claude-opus-4-6": {
        "input_per_1m": 15.00,
        "output_per_1m": 75.00,
        "cache_write_mult": 1.25,
        "cache_read_mult": 0.10,
    },
}


def _compute_cost_usd(usage: dict) -> float:
    """Compute USD cost from an Anthropic usage dict.

    usage keys consumed:
      - model (required to look up pricing; unknown → $0)
      - input_tokens, output_tokens
      - cache_creation_input_tokens (billed at cache_write_mult × input rate)
      - cache_read_input_tokens (billed at cache_read_mult × input rate)
    """
    p = PRICING.get(usage.get("model"))
    if not p:
        return 0.0
    in_tok = usage.get("input_tokens", 0) or 0
    out_tok = usage.get("output_tokens", 0) or 0
    cw = usage.get("cache_creation_input_tokens", 0) or 0
    cr = usage.get("cache_read_input_tokens", 0) or 0
    cost = (
        in_tok * p["input_per_1m"] / 1_000_000
        + out_tok * p["output_per_1m"] / 1_000_000
        + cw * p["input_per_1m"] * p["cache_write_mult"] / 1_000_000
        + cr * p["input_per_1m"] * p["cache_read_mult"] / 1_000_000
    )
    return round(cost, 6)


def _today_spend_usd(con: sqlite3.Connection) -> float:
    """Sum today's extraction_metrics.cost_usd (UTC date window)."""
    row = con.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM extraction_metrics "
        "WHERE created_at >= DATE('now')"
    ).fetchone()
    return float(row[0] or 0.0)


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


def _extract_tool_with_cache() -> dict:
    """Return EXTRACT_TOOL with cache_control marker on the definition.

    Cache-control on tool schemas saves ~600 tokens per call — the JSON Schema
    for save_extraction is long and identical every call.
    """
    return {**tool_schemas.EXTRACT_TOOL, "cache_control": {"type": "ephemeral"}}


def _triage_tool_with_cache() -> dict:
    return {**tool_schemas.TRIAGE_TOOL, "cache_control": {"type": "ephemeral"}}


def call_sonnet_triage(prompt: str, expected_count: int,
                       dynamic_suffix: str | None = None) -> tuple[list[dict], dict]:
    """Call Sonnet triage with prompt caching.

    Backward-compatible: if `dynamic_suffix` is None, the caller passed a
    single combined string via `prompt` and we split it heuristically — the
    full string is sent as dynamic (no cache hit, but also no regression).

    Preferred: pass `prompt` = static prefix and `dynamic_suffix` = dynamic tail
    (as returned by `prompts.build_triage_prompt_parts`).
    """
    client = _anthropic_client()
    if dynamic_suffix is None:
        # Legacy single-string path. Don't try to cache — we don't know the split.
        content = [{"type": "text", "text": prompt}]
    else:
        content = [
            {"type": "text", "text": prompt,
             "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": dynamic_suffix},
        ]
    msg = client.messages.create(
        model=TRIAGE_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": content}],
        tools=[_triage_tool_with_cache()],
        tool_choice={"type": "tool", "name": "triage_observations"},
    )
    usage = _usage_from_msg(msg, TRIAGE_MODEL)
    for block in msg.content:
        if block.type == "tool_use" and block.name == "triage_observations":
            return block.input["verdicts"], usage
    raise ValueError("Sonnet did not call triage_observations tool")


def call_opus_extract(prompt: str, dynamic_suffix: str | None = None) -> tuple[dict, dict]:
    """Call Opus extract with prompt caching.

    Same shape as `call_sonnet_triage`: either pass a single combined prompt
    (legacy, uncached) or pass `prompt` = static prefix + `dynamic_suffix` =
    variable tail (cached static prefix).
    """
    client = _anthropic_client()
    if dynamic_suffix is None:
        content = [{"type": "text", "text": prompt}]
    else:
        content = [
            {"type": "text", "text": prompt,
             "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": dynamic_suffix},
        ]
    msg = client.messages.create(
        model=EXTRACT_MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": content}],
        tools=[_extract_tool_with_cache()],
        tool_choice={"type": "tool", "name": "save_extraction"},
    )
    usage = _usage_from_msg(msg, EXTRACT_MODEL)
    for block in msg.content:
        if block.type == "tool_use" and block.name == "save_extraction":
            return block.input, usage
    raise ValueError("Opus did not call save_extraction tool")


def _usage_from_msg(msg, model: str) -> dict:
    """Extract usage dict from an Anthropic message, including cache tokens.

    Cache counters are on usage for cached calls; absent on uncached. Use
    getattr with a 0 default so the mock-heavy test suite (which doesn't set
    these) still computes correct costs from input/output.
    """
    u = msg.usage
    return {
        "input_tokens": getattr(u, "input_tokens", 0) or 0,
        "output_tokens": getattr(u, "output_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        "model": model,
    }


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
    """Full-table scan of entities. Used for resolver matching inside
    `_apply_extraction` where we need ALL candidate entities to decide
    bind_identity vs proposal. DO NOT use in prompt construction — use
    `_load_candidate_entities` for that (top-K by relevance).
    """
    rows = con.execute("SELECT id, canonical_name, kind, aliases FROM entities").fetchall()
    return [
        {"id": r[0], "canonical_name": r[1], "kind": r[2], "aliases": json.loads(r[3] or "[]")}
        for r in rows
    ]


# Shared tokenizer with extract/retrieval.py (`_tokenize`). Keep the regex in
# sync — it's the contract for what counts as a "name-like" token.
_TOKEN_RE = re.compile(r"\b[\w\-]{2,}\b", re.UNICODE)


def _tokenize_observation(text: str) -> set[str]:
    """Lowercased unigram tokens suitable for candidate-entity matching."""
    if not text:
        return set()
    return {w.lower() for w in _TOKEN_RE.findall(text)}


def _load_candidate_entities(
    con: sqlite3.Connection, observation: dict, top_k: int = 50
) -> list[dict]:
    """Return top-K entities relevant to `observation` for the Opus prompt.

    Ranking (two-pass, deterministic):
      1. Entities whose canonical_name or any alias shares a token with
         `observation.content_text`, OR whose canonical_name/alias appears in
         `observation.actors[*].id`. Ordered by salience DESC, last_seen DESC.
      2. If we still have room under `top_k`, pad with globally top entities
         by salience DESC, last_seen DESC (for merge/resolution quality).

    The full-table `_load_existing_entities` is still used by `_apply_extraction`
    for resolver decisions — don't replace it there.
    """
    tokens = _tokenize_observation(observation.get("content_text") or "")
    actor_ids = {
        str(a.get("id", "")).lower()
        for a in (observation.get("actors") or [])
        if a.get("id") is not None
    }
    match_set = tokens | actor_ids

    rows = con.execute(
        "SELECT id, canonical_name, kind, aliases, salience_score, last_seen "
        "FROM entities ORDER BY salience_score DESC, last_seen DESC"
    ).fetchall()

    matched: list[dict] = []
    matched_ids: set[int] = set()
    leftovers: list[dict] = []

    for eid, name, kind, aliases_json, _sal, _ls in rows:
        try:
            aliases = json.loads(aliases_json or "[]")
        except (json.JSONDecodeError, TypeError):
            aliases = []
        ent = {"id": eid, "canonical_name": name, "kind": kind, "aliases": aliases}

        names_lc = {(name or "").lower()} | {(a or "").lower() for a in aliases}
        if match_set and names_lc & match_set:
            matched.append(ent)
            matched_ids.add(eid)
        else:
            leftovers.append(ent)

        if len(matched) >= top_k:
            break

    if len(matched) < top_k:
        for ent in leftovers:
            if ent["id"] in matched_ids:
                continue
            matched.append(ent)
            if len(matched) >= top_k:
                break

    return matched[:top_k]


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
        "event_entities_written": 0,
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
                    "INSERT INTO entities (canonical_name, kind, aliases, first_seen, last_seen, salience_score, emotional_weight, scorer_version, extractor_version) VALUES (?,?,?,?,?,?,?,?,?)",
                    (ent["canonical_name"], ent.get("kind", "person"), json.dumps(ent.get("aliases") or []),
                     now, now, scored["salience_score"], scored["emotional_weight"], scored["scorer_version"], "v2"),
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
        except Exception as ex:
            con.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            con.execute(f"RELEASE SAVEPOINT {sp}")
            _item_failure("entity", f"{type(ex).__name__}: {str(ex)[:200]}",
                          {"index": idx, "canonical_name": ent.get("canonical_name", ""), "kind": ent.get("kind", "")})

    # --- events ---
    for idx, ev in enumerate(result.get("events", [])):
        involved = ev.get("entities_involved") or []
        if not involved:
            _item_failure("event", "orphan_event_no_entities_involved", {"title": ev.get("title", "")})
            continue
        resolved_entity_ids = [name_to_id[n] for n in involved if n in name_to_id]
        if not resolved_entity_ids:
            _item_failure(
                "event", "all_entities_involved_unresolved",
                {"index": idx, "title": ev.get("title", ""), "names": involved},
            )
            continue
        sp = f"ev_{idx}"
        con.execute(f"SAVEPOINT {sp}")
        try:
            s = scorer.score_event(ev)
            cur = con.execute(
                "INSERT INTO events (title, description, sentiment, emotional_weight, scorer_version, ts) VALUES (?,?,?,?,?,?)",
                (ev.get("title", ""), ev.get("description", ""), s["sentiment"], s["emotional_weight"], s["scorer_version"], ev.get("ts", now)),
            )
            event_id = cur.lastrowid
            for ent_id in resolved_entity_ids:
                con.execute(
                    "INSERT OR IGNORE INTO event_entities (event_id, entity_id) VALUES (?, ?)",
                    (event_id, ent_id),
                )
                report["event_entities_written"] += 1
            con.execute(
                "INSERT INTO evidence (subject_kind, subject_id, observation_id, created_at) VALUES ('event',?,?,?)",
                (event_id, obs_id, now),
            )
            con.execute(f"RELEASE SAVEPOINT {sp}")
            report["events_written"] += 1
        except Exception as ex:
            con.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            con.execute(f"RELEASE SAVEPOINT {sp}")
            _item_failure("event", f"{type(ex).__name__}: {str(ex)[:200]}",
                          {"index": idx, "title": ev.get("title", "")})

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
                """INSERT INTO relations (from_entity_id, to_entity_id, kind, strength, first_seen, last_seen, context)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(from_entity_id, to_entity_id, kind) DO UPDATE SET
                       strength  = strength + 1,
                       last_seen = excluded.last_seen,
                       context   = COALESCE(excluded.context, context)""",
                (from_id, to_id, rel.get("kind", ""), float(rel.get("strength", 0.0)), now, now, rel.get("context")),
            )
            con.execute(
                "INSERT INTO evidence (subject_kind, subject_id, observation_id, created_at) VALUES ('relation',?,?,?)",
                (cur.lastrowid, obs_id, now),
            )
            con.execute(f"RELEASE SAVEPOINT {sp}")
            report["relations_written"] += 1
        except Exception as ex:
            con.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            con.execute(f"RELEASE SAVEPOINT {sp}")
            _item_failure("relation", f"{type(ex).__name__}: {str(ex)[:200]}",
                          {"index": idx, "from": rel.get("from", ""), "to": rel.get("to", ""), "kind": rel.get("kind", "")})

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
                """INSERT INTO facts (entity_id, text, confidence, scorer_version, source_obs_id, extractor_version, created_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(entity_id, text) DO NOTHING""",
                (entity_id, fact.get("text", ""), scored["confidence"], scored["scorer_version"], obs_id, "v2", now),
            )
            if cur.rowcount == 1:
                con.execute(
                    "INSERT INTO evidence (subject_kind, subject_id, observation_id, created_at) VALUES ('fact',?,?,?)",
                    (cur.lastrowid, obs_id, now),
                )
                report["facts_written"] += 1
            con.execute(f"RELEASE SAVEPOINT {sp}")
        except Exception as ex:
            con.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            con.execute(f"RELEASE SAVEPOINT {sp}")
            _item_failure("fact", f"{type(ex).__name__}: {str(ex)[:200]}",
                          {"index": idx, "entity": fact.get("entity", ""), "text": fact.get("text", "")[:80]})

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


def _get_artifact(con: sqlite3.Connection, job_id: int, kind: str,
                  obs_id: int | None) -> dict | None:
    """Return the parsed payload_json for a (job_id, kind, obs_id) artifact, or None."""
    if obs_id is None:
        row = con.execute(
            "SELECT payload_json FROM extraction_artifacts WHERE job_id=? AND kind=? AND obs_id IS NULL",
            (job_id, kind),
        ).fetchone()
    else:
        row = con.execute(
            "SELECT payload_json FROM extraction_artifacts WHERE job_id=? AND kind=? AND obs_id=?",
            (job_id, kind, obs_id),
        ).fetchone()
    return json.loads(row[0]) if row else None


def _save_artifact(con: sqlite3.Connection, job_id: int, kind: str,
                   obs_id: int | None, payload: dict, model: str) -> None:
    """Persist a checkpoint artifact in its own committed tx.

    First-write-wins: partial UNIQUE indices + INSERT OR IGNORE keep one
    row per (job_id,kind,obs_id). Re-saving the same triple is a safe no-op
    — the caller may be replaying after a crash where the artifact was
    already committed but the downstream work (apply) hadn't finished.
    """
    con.execute("BEGIN IMMEDIATE")
    con.execute(
        "INSERT OR IGNORE INTO extraction_artifacts(job_id, kind, obs_id, payload_json, model) "
        "VALUES (?, ?, ?, ?, ?)",
        (job_id, kind, obs_id, json.dumps(payload, ensure_ascii=False), model),
    )
    con.execute("COMMIT")


def _save_metrics(con: sqlite3.Connection, job_id: int, usage: dict) -> None:
    """Insert one extraction_metrics row, including computed cost_usd.

    cost_usd is derived from usage + PRICING; we always write it (0.0 for
    unknown models) so the budget gate has a reliable SUM to read.
    """
    cost = _compute_cost_usd(usage)
    con.execute(
        "INSERT INTO extraction_metrics (job_id, model, input_tokens, output_tokens, cost_usd) "
        "VALUES (?,?,?,?,?)",
        (
            job_id,
            usage.get("model", "unknown"),
            usage.get("input_tokens"),
            usage.get("output_tokens"),
            cost,
        ),
    )


def run_once(db_path: str, budget_usd_remaining: float = 10.0) -> int:
    con = _open_connection(db_path)

    try:
        if budget_usd_remaining <= 0:
            print("budget exhausted for today — skipping extraction run")
            return 0

        # Pre-flight live budget gate: today's committed spend + worst-case
        # next-job estimate must fit under budget. Prevents the pathological
        # cron-every-2-min backlog scenario from burning hundreds of dollars
        # before anyone notices.
        today_spend = _today_spend_usd(con)
        if today_spend + WORST_CASE_NEXT_JOB_USD > budget_usd_remaining:
            print(
                f"budget gate fired: today=${today_spend:.2f}, "
                f"next-job-worst=${WORST_CASE_NEXT_JOB_USD:.2f}, "
                f"budget=${budget_usd_remaining:.2f}"
            )
            return 0

        jobs = con.execute(
            "SELECT id, observation_ids FROM extraction_jobs "
            "WHERE state='pending' ORDER BY created_at LIMIT 10"
        ).fetchall()
        if not jobs:
            print("no pending jobs")
            return 0

        for job_id, obs_ids_json in jobs:
            # Mid-batch recheck: after each finished job we may have written
            # $$ to extraction_metrics. If we've crossed budget, abort the
            # remaining pending jobs (they stay pending, re-tried next run).
            today_spend = _today_spend_usd(con)
            if today_spend + WORST_CASE_NEXT_JOB_USD > budget_usd_remaining:
                print(
                    f"budget gate fired mid-batch: today=${today_spend:.2f}, "
                    f"next-job-worst=${WORST_CASE_NEXT_JOB_USD:.2f}, "
                    f"budget=${budget_usd_remaining:.2f}"
                )
                return 0

            obs_ids = json.loads(obs_ids_json)
            _set_job_state(con, job_id, "running", increment_attempts=True)

            observations = _load_observations(con, obs_ids)
            if not observations:
                _set_job_state(con, job_id, "failed", last_error="no observations")
                continue

            try:
                verdicts = _get_artifact(con, job_id, "triage", None)
                if verdicts is None:
                    triage_static, triage_dynamic = prompts.build_triage_prompt_parts(observations)
                    verdicts, triage_usage = call_sonnet_triage(
                        triage_static, expected_count=len(observations),
                        dynamic_suffix=triage_dynamic,
                    )
                    _save_artifact(con, job_id, "triage", None, verdicts, TRIAGE_MODEL)
                    _save_metrics(con, job_id, triage_usage)
                job_reports: list[dict] = []

                if len(verdicts) != len(observations):
                    raise ValueError(
                        f"triage returned {len(verdicts)} verdicts for {len(observations)} observations"
                    )
                for obs, v in zip(observations, verdicts):
                    if v["verdict"] != "extract":
                        continue
                    result = _get_artifact(con, job_id, "extract", obs["id"])
                    if result is None:
                        candidates = _load_candidate_entities(con, obs, top_k=50)
                        graph_ctx = {"existing_entities": candidates}
                        extract_static, extract_dynamic = prompts.build_extract_prompt_parts(
                            obs, graph_ctx
                        )
                        result, extract_usage = call_opus_extract(
                            extract_static, dynamic_suffix=extract_dynamic,
                        )
                        _save_artifact(con, job_id, "extract", obs["id"], result, EXTRACT_MODEL)
                        _save_metrics(con, job_id, extract_usage)

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
                            "event_entities_written": 0,
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
                    triage_model=TRIAGE_MODEL, extract_model=EXTRACT_MODEL,
                )
                print(f"job {job_id}: done, apply_report={json.dumps(job_reports, ensure_ascii=False)[:500]}")
            except Exception as e:
                attempts_row = con.execute(
                    "SELECT attempts FROM extraction_jobs WHERE id=?", (job_id,)
                ).fetchone()
                attempts = attempts_row[0] if attempts_row else 0
                next_state = "dlq" if attempts >= 3 else "pending"
                _set_job_state(con, job_id, next_state, last_error=str(e))
                print(f"job {job_id}: {next_state}, reason={type(e).__name__}: {str(e)[:200]}")
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
