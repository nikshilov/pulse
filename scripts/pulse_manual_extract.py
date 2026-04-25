#!/usr/bin/env python3
"""pulse_manual_extract - reproducible Codex/manual extraction batches.

This is the "run it through Codex/me" path without pretending there is a
background model call inside Pulse. It has two modes:

  prepare: export pending observations into a JSON work file.
  apply:   validate a filled work file and apply it through pulse_extract.

The apply path uses the same _apply_extraction function as the LLM extractor,
so evidence rows, graph_snapshots, resolver/scorer behavior, and job state
transitions stay identical to the production pipeline.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pulse_extract  # noqa: E402
from extract.embedder import FAKE_LOCAL_MODEL, embed_texts, embedding_dim  # noqa: E402


SCHEMA = "pulse_manual_extract.v1"
DEFAULT_MODEL = "codex-manual"
EMOTION_KEYS = (
    "joy", "sadness", "anger", "fear", "trust",
    "disgust", "anticipation", "surprise", "shame", "guilt",
)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _parse_json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _parse_ids(raw: str | None) -> list[int]:
    if not raw:
        return []
    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        ids.append(int(part))
    return ids


def _empty_extraction() -> dict[str, list]:
    return {
        "entities": [],
        "relations": [],
        "events": [],
        "facts": [],
        "merge_candidates": [],
    }


def _template_row(row: sqlite3.Row) -> dict:
    actors = _parse_json(row["actors"], [])
    metadata = _parse_json(row["metadata"], {})
    return {
        "obs_id": row["id"],
        "job_id": row["job_id"],
        "job_state": row["job_state"],
        "source_kind": row["source_kind"],
        "source_id": row["source_id"],
        "observed_at": row["observed_at"],
        "actors": actors,
        "metadata": metadata,
        "content_text": row["content_text"],
        "triage": {
            "verdict": "extract",
            "reason": "",
        },
        "extraction": _empty_extraction(),
        "event_emotions": [],
        "event_chains": [],
    }


def prepare_batch(
    db_path: str,
    *,
    ids: list[int] | None = None,
    contains: list[str] | None = None,
    state: str = "pending",
    limit: int = 20,
) -> dict:
    """Return a manual extraction work file payload."""
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA busy_timeout=5000")
    con.row_factory = sqlite3.Row
    try:
        clauses = ["1=1"]
        args: list[Any] = []

        if ids:
            placeholders = ",".join("?" for _ in ids)
            clauses.append(f"o.id IN ({placeholders})")
            args.extend(ids)

        for needle in contains or []:
            clauses.append("o.content_text LIKE ?")
            args.append(f"%{needle}%")

        if state != "any":
            clauses.append("j.state = ?")
            args.append(state)

        sql = f"""
            SELECT
                o.id, o.source_kind, o.source_id, o.observed_at, o.actors,
                o.metadata, o.content_text,
                j.id AS job_id, j.state AS job_state
            FROM observations o
            JOIN extraction_jobs j ON j.observation_ids = printf('[%d]', o.id)
            WHERE {' AND '.join(clauses)}
            ORDER BY o.id
            LIMIT ?
        """
        args.append(limit)
        rows = con.execute(sql, args).fetchall()
    finally:
        con.close()

    return {
        "schema": SCHEMA,
        "model": DEFAULT_MODEL,
        "created_at": _now(),
        "db_path_hint": str(Path(db_path).expanduser()),
        "instructions": (
            "Fill each observation.extraction with entities, relations, events, "
            "and facts using the pulse_extract tool schema. Relation/event/fact "
            "entity references must be present in the same extraction.entities "
            "list, either as canonical_name or alias."
        ),
        "observations": [_template_row(row) for row in rows],
    }


def _entity_names(result: dict) -> set[str]:
    names: set[str] = set()
    for ent in result.get("entities", []):
        canonical = ent.get("canonical_name")
        if canonical:
            names.add(canonical)
        for alias in ent.get("aliases") or []:
            if alias:
                names.add(alias)
    return names


def validate_extraction(result: dict, *, obs_id: int) -> None:
    """Fail early on references that _apply_extraction cannot resolve."""
    for key in ("entities", "relations", "events", "facts"):
        if key not in result or not isinstance(result[key], list):
            raise ValueError(f"obs {obs_id}: extraction.{key} must be a list")

    names = _entity_names(result)
    if result["relations"] or result["events"] or result["facts"]:
        if not names:
            raise ValueError(f"obs {obs_id}: referenced items require extraction.entities")

    for idx, ent in enumerate(result["entities"]):
        if not ent.get("canonical_name"):
            raise ValueError(f"obs {obs_id}: entity {idx} missing canonical_name")
        if not ent.get("kind"):
            raise ValueError(f"obs {obs_id}: entity {ent.get('canonical_name')} missing kind")

    for idx, rel in enumerate(result["relations"]):
        for side in ("from", "to"):
            ref = rel.get(side)
            if ref not in names:
                raise ValueError(f"obs {obs_id}: relation {idx} unknown {side} entity {ref!r}")

    for idx, event in enumerate(result["events"]):
        if not event.get("title"):
            raise ValueError(f"obs {obs_id}: event {idx} missing title")
        involved = event.get("entities_involved") or []
        if not involved:
            raise ValueError(f"obs {obs_id}: event {event.get('title')!r} has no entities_involved")
        for ref in involved:
            if ref not in names:
                raise ValueError(f"obs {obs_id}: event {idx} unknown involved entity {ref!r}")

    for idx, fact in enumerate(result["facts"]):
        ref = fact.get("entity")
        if ref not in names:
            raise ValueError(f"obs {obs_id}: fact {idx} unknown entity {ref!r}")
        if not fact.get("text"):
            raise ValueError(f"obs {obs_id}: fact {idx} missing text")


def _load_job_state(con: sqlite3.Connection, job_id: int) -> str | None:
    row = con.execute("SELECT state FROM extraction_jobs WHERE id=?", (job_id,)).fetchone()
    return row[0] if row else None


def _ensure_single_observation_job(con: sqlite3.Connection, obs_id: int, job_id: int) -> None:
    row = con.execute("SELECT observation_ids FROM extraction_jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise ValueError(f"obs {obs_id}: extraction job {job_id} not found")
    try:
        obs_ids = json.loads(row[0])
    except json.JSONDecodeError as exc:
        raise ValueError(f"job {job_id}: invalid observation_ids JSON") from exc
    if obs_ids != [obs_id]:
        raise ValueError(
            f"obs {obs_id}: manual apply expects one-observation jobs, "
            f"job {job_id} has {obs_ids}"
        )


def _event_ids_for_observation(con: sqlite3.Connection, obs_id: int) -> list[int]:
    rows = con.execute(
        """
        SELECT DISTINCT e.id
        FROM events e
        JOIN evidence ev ON ev.subject_kind='event' AND ev.subject_id=e.id
        WHERE ev.observation_id=?
        ORDER BY e.id
        """,
        (obs_id,),
    ).fetchall()
    return [int(row[0]) for row in rows]


def _seed_fake_event_embeddings(con: sqlite3.Connection, obs_id: int) -> int:
    event_ids = _event_ids_for_observation(con, obs_id)
    if not event_ids:
        return 0

    dim = embedding_dim(FAKE_LOCAL_MODEL)
    written = 0
    con.execute("BEGIN IMMEDIATE")
    try:
        for event_id in event_ids:
            row = con.execute(
                "SELECT title, COALESCE(description, '') FROM events WHERE id=?",
                (event_id,),
            ).fetchone()
            if not row:
                continue
            text = f"{row[0]}\n{row[1]}".strip()
            vec = embed_texts([text], model=FAKE_LOCAL_MODEL)[0]
            con.execute(
                """
                INSERT OR REPLACE INTO event_embeddings
                    (event_id, model, dim, vector_json, text_source, updated_at)
                VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                """,
                (event_id, FAKE_LOCAL_MODEL, dim, json.dumps(vec), text),
            )
            written += 1
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return written


def _title_to_event_id(con: sqlite3.Connection) -> dict[str, int]:
    rows = con.execute("SELECT id, title FROM events ORDER BY id").fetchall()
    return {str(title): int(event_id) for event_id, title in rows}


def _apply_event_emotions(con: sqlite3.Connection, item: dict, model: str) -> int:
    emotions = item.get("event_emotions") or []
    if not emotions:
        return 0
    title_map = _title_to_event_id(con)
    written = 0
    con.execute("BEGIN IMMEDIATE")
    try:
        for entry in emotions:
            title = entry.get("event_title") or entry.get("title") or entry.get("event")
            event_id = entry.get("event_id") or title_map.get(title)
            if not event_id:
                raise ValueError(f"obs {item.get('obs_id')}: unknown event_emotion target {title!r}")
            values = [float(entry.get(key, 0.0)) for key in EMOTION_KEYS]
            for key, value in zip(EMOTION_KEYS, values):
                if value < 0.0 or value > 1.0:
                    raise ValueError(f"obs {item.get('obs_id')}: emotion {key} out of range")
            con.execute(
                """
                INSERT OR REPLACE INTO event_emotions
                    (event_id, joy, sadness, anger, fear, trust, disgust,
                     anticipation, surprise, shame, guilt, tagger,
                     tagger_version, confidence, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                """,
                (
                    event_id,
                    *values,
                    "manual",
                    model,
                    float(entry.get("confidence", 1.0)),
                ),
            )
            written += 1
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return written


def _apply_event_chains(con: sqlite3.Connection, item: dict) -> int:
    chains = item.get("event_chains") or []
    if not chains:
        return 0
    title_map = _title_to_event_id(con)
    written = 0
    con.execute("BEGIN IMMEDIATE")
    try:
        for chain in chains:
            parent_title = chain.get("parent_title") or chain.get("parent")
            child_title = chain.get("child_title") or chain.get("child")
            parent_id = chain.get("parent_id") or title_map.get(parent_title)
            child_id = chain.get("child_id") or title_map.get(child_title)
            if not parent_id or not child_id:
                raise ValueError(
                    f"obs {item.get('obs_id')}: unknown chain endpoints "
                    f"{parent_title!r} -> {child_title!r}"
                )
            con.execute(
                """
                INSERT OR REPLACE INTO event_chains(parent_id, child_id, strength, kind)
                VALUES (?, ?, ?, ?)
                """,
                (
                    parent_id,
                    child_id,
                    float(chain.get("strength", 1.0)),
                    chain.get("kind", "causal"),
                ),
            )
            written += 1
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return written


def apply_batch(
    db_path: str,
    batch: dict,
    *,
    model: str = DEFAULT_MODEL,
    dry_run: bool = False,
    force: bool = False,
    fake_embeddings: bool = False,
) -> dict:
    if batch.get("schema") != SCHEMA:
        raise ValueError(f"unsupported schema: {batch.get('schema')!r}")

    observations = batch.get("observations")
    if not isinstance(observations, list):
        raise ValueError("batch.observations must be a list")

    con = pulse_extract._open_connection(db_path)
    summary = {
        "checked": 0,
        "applied": 0,
        "skipped": 0,
        "deferred": 0,
        "fake_embeddings_written": 0,
        "event_emotions_written": 0,
        "event_chains_written": 0,
        "reports": [],
    }
    try:
        for item in observations:
            obs_id = int(item["obs_id"])
            job_id = int(item["job_id"])
            summary["checked"] += 1
            _ensure_single_observation_job(con, obs_id, job_id)

            triage = item.get("triage") or {}
            verdict = triage.get("verdict", "extract")
            reason = triage.get("reason", "manual Codex extraction")
            if verdict not in {"extract", "skip", "defer"}:
                raise ValueError(f"obs {obs_id}: invalid triage verdict {verdict!r}")

            result = item.get("extraction") or _empty_extraction()
            if verdict == "extract":
                validate_extraction(result, obs_id=obs_id)

            if dry_run:
                continue

            state = _load_job_state(con, job_id)
            if state == "done" and not force:
                summary["skipped"] += 1
                summary["reports"].append({"obs_id": obs_id, "job_id": job_id, "skipped": "already_done"})
                continue

            if verdict == "defer":
                summary["deferred"] += 1
                summary["reports"].append({"obs_id": obs_id, "job_id": job_id, "deferred": reason})
                continue

            pulse_extract._set_job_state(con, job_id, "running", increment_attempts=True)
            pulse_extract._save_artifact(
                con,
                job_id,
                "triage",
                None,
                [{"index": 1, "verdict": verdict, "reason": reason}],
                model,
            )

            if verdict == "skip":
                pulse_extract._set_job_state(con, job_id, "done", triage_model=model, extract_model=model)
                summary["skipped"] += 1
                summary["reports"].append({"obs_id": obs_id, "job_id": job_id, "skipped": reason})
                continue

            pulse_extract._save_artifact(con, job_id, "extract", obs_id, result, model)
            con.execute("BEGIN IMMEDIATE")
            try:
                report = pulse_extract._apply_extraction(con, obs_id, result)
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                pulse_extract._set_job_state(con, job_id, "pending", last_error="manual apply failed")
                raise

            if fake_embeddings:
                summary["fake_embeddings_written"] += _seed_fake_event_embeddings(con, obs_id)
            summary["event_emotions_written"] += _apply_event_emotions(con, item, model)
            summary["event_chains_written"] += _apply_event_chains(con, item)
            pulse_extract._set_job_state(con, job_id, "done", triage_model=model, extract_model=model)
            report["job_id"] = job_id
            summary["reports"].append(report)
            summary["applied"] += 1
    finally:
        con.close()
    return summary


def _read_batch(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_batch(path: Path, payload: dict, *, force: bool = False) -> None:
    if path.exists() and not force:
        raise FileExistsError(f"{path} already exists; pass --force to overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def cmd_prepare(args: argparse.Namespace) -> int:
    payload = prepare_batch(
        args.db,
        ids=_parse_ids(args.ids),
        contains=args.contains or [],
        state=args.state,
        limit=args.limit,
    )
    if args.out:
        _write_batch(Path(args.out), payload, force=args.force)
        print(f"wrote {len(payload['observations'])} observations to {args.out}")
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    batch = _read_batch(Path(args.file))
    summary = apply_batch(
        args.db,
        batch,
        model=args.model,
        dry_run=args.dry_run,
        force=args.force,
        fake_embeddings=args.fake_embeddings,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pulse_manual_extract",
        description="Prepare/apply reproducible Codex/manual extraction batches.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_prepare = sub.add_parser("prepare", help="export observations into a work file")
    p_prepare.add_argument("--db", required=True)
    p_prepare.add_argument("--ids", help="comma-separated observation ids")
    p_prepare.add_argument("--contains", action="append", help="content_text substring filter; repeatable")
    p_prepare.add_argument("--state", default="pending", choices=["pending", "running", "done", "dlq", "any"])
    p_prepare.add_argument("--limit", type=int, default=20)
    p_prepare.add_argument("--out")
    p_prepare.add_argument("--force", action="store_true")
    p_prepare.set_defaults(func=cmd_prepare)

    p_apply = sub.add_parser("apply", help="apply a filled manual extraction work file")
    p_apply.add_argument("--db", required=True)
    p_apply.add_argument("--file", required=True)
    p_apply.add_argument("--model", default=DEFAULT_MODEL)
    p_apply.add_argument("--dry-run", action="store_true")
    p_apply.add_argument("--force", action="store_true", help="re-apply even if job is already done")
    p_apply.add_argument("--fake-embeddings", action="store_true", help="seed fake-local embeddings for inserted events")
    p_apply.set_defaults(func=cmd_apply)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
