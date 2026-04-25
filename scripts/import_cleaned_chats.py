#!/usr/bin/env python3
"""Import cleaned Claude chat markdown dumps into Pulse observations.

The raw Claude JSONL provider stores every visible user/assistant message.
That is useful for provenance, but expensive and noisy for memory extraction:
Claude Code status messages such as "Now let me check..." become separate
jobs. This importer treats an already-cleaned markdown chat as the unit of
memory, then chunks very large chats on turn boundaries.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SOURCE_KIND = "claude_cleaned_md"
TURN_RE = re.compile(r"^##\s+\d+\.\s+(User|Assistant)(?:\s+(?:--|—)\s+(.+))?\s*$")


@dataclass
class Turn:
    role: str
    timestamp: str
    text: str


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _file_time(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _content_hash(content: str, metadata: dict[str, Any]) -> str:
    h = hashlib.sha256()
    h.update(content.encode("utf-8"))
    h.update(b"\x1f")
    canonical = [[k, metadata[k]] for k in sorted(metadata.keys())]
    h.update(json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return h.hexdigest()


def parse_turns(path: Path) -> list[Turn]:
    turns: list[Turn] = []
    current_role: str | None = None
    current_ts = ""
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_role, current_ts, current_lines
        if current_role is None:
            return
        text = "\n".join(current_lines).strip()
        if text:
            turns.append(Turn(role=current_role, timestamp=current_ts, text=text))
        current_role = None
        current_ts = ""
        current_lines = []

    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        match = TURN_RE.match(line)
        if match:
            flush()
            current_role = match.group(1)
            current_ts = (match.group(2) or "").strip()
            continue
        if current_role is not None:
            current_lines.append(line)
    flush()
    return turns


def chunk_turns(turns: list[Turn], max_chars: int) -> list[list[Turn]]:
    chunks: list[list[Turn]] = []
    current: list[Turn] = []
    current_len = 0
    for turn in turns:
        rendered_len = len(render_turn(turn)) + 2
        if current and current_len + rendered_len > max_chars:
            chunks.append(current)
            current = []
            current_len = 0
        current.append(turn)
        current_len += rendered_len
    if current:
        chunks.append(current)
    return chunks


def render_turn(turn: Turn) -> str:
    stamp = f" -- {turn.timestamp}" if turn.timestamp else ""
    return f"## {turn.role}{stamp}\n\n{turn.text.strip()}"


def render_chunk(path: Path, rel: Path, index: int, total: int, turns: list[Turn]) -> str:
    lines = [
        f"# Cleaned Claude chat chunk {index + 1}/{total}",
        "",
        f"- Cleaned source: `{rel}`",
        f"- Original file: `{path}`",
        "",
        "---",
        "",
    ]
    lines.extend(render_turn(turn) for turn in turns)
    return "\n\n".join(lines).strip() + "\n"


def _actors_for(turns: list[Turn]) -> list[dict[str, str]]:
    roles = {turn.role for turn in turns}
    actors: list[dict[str, str]] = []
    if "User" in roles:
        actors.append({"kind": "user", "id": "nik"})
    if "Assistant" in roles:
        actors.append({"kind": "assistant", "id": "claude"})
    return actors or [{"kind": "assistant", "id": "claude"}]


def _observed_at(turns: list[Turn], fallback: str) -> str:
    for turn in turns:
        if turn.timestamp:
            return turn.timestamp
    return fallback


def iter_observations(root: Path, max_chars: int) -> list[dict[str, Any]]:
    files = sorted(path for path in root.rglob("*.md") if path.name != "INDEX.md")
    out: list[dict[str, Any]] = []
    for path in files:
        turns = parse_turns(path)
        if not turns:
            continue
        rel = path.relative_to(root)
        fallback_ts = _file_time(path)
        chunks = chunk_turns(turns, max_chars=max_chars)
        for idx, chunk in enumerate(chunks):
            metadata = {
                "source_root": str(root),
                "source_file": str(path),
                "relative_path": str(rel),
                "chunk_index": idx,
                "chunk_count": len(chunks),
                "turn_count": len(chunk),
                "cleaned_chat": True,
            }
            content = render_chunk(path, rel, idx, len(chunks), chunk)
            source_id = f"{rel}#chunk-{idx + 1:04d}"
            out.append({
                "source_kind": SOURCE_KIND,
                "source_id": source_id,
                "content_hash": _content_hash(content, metadata),
                "version": 1,
                "scope": "shared",
                "captured_at": fallback_ts,
                "observed_at": _observed_at(chunk, fallback_ts),
                "actors": _actors_for(chunk),
                "content_text": content,
                "metadata": metadata,
                "raw_json": {"importer": "import_cleaned_chats.py"},
            })
    return out


def import_observations(db_path: str, observations: list[dict[str, Any]], *, dry_run: bool = False) -> dict[str, int]:
    stats = {"inserted": 0, "duplicates": 0, "revisions": 0, "jobs": 0}
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA busy_timeout=5000")
    try:
        for obs in observations:
            row = con.execute(
                """
                SELECT id, content_hash, version FROM observations
                WHERE source_kind=? AND source_id=?
                ORDER BY version DESC LIMIT 1
                """,
                (obs["source_kind"], obs["source_id"]),
            ).fetchone()
            if row and row[1] == obs["content_hash"]:
                stats["duplicates"] += 1
                continue

            version = int(row[2]) + 1 if row else 1
            if dry_run:
                stats["revisions" if row else "inserted"] += 1
                stats["jobs"] += 1
                continue

            now = _now()
            cur = con.execute(
                """
                INSERT INTO observations
                  (source_kind, source_id, content_hash, version, scope,
                   captured_at, observed_at, actors, content_text, media_refs,
                   metadata, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    obs["source_kind"],
                    obs["source_id"],
                    obs["content_hash"],
                    version,
                    obs["scope"],
                    obs["captured_at"],
                    obs["observed_at"],
                    json.dumps(obs["actors"], ensure_ascii=False),
                    obs["content_text"],
                    "[]",
                    json.dumps(obs["metadata"], ensure_ascii=False, sort_keys=True),
                    json.dumps(obs["raw_json"], ensure_ascii=False, sort_keys=True),
                ),
            )
            obs_id = cur.lastrowid
            con.execute(
                """
                INSERT INTO extraction_jobs (observation_ids, state, attempts, created_at, updated_at)
                VALUES (?, 'pending', 0, ?, ?)
                """,
                (json.dumps([obs_id]), now, now),
            )
            stats["revisions" if row else "inserted"] += 1
            stats["jobs"] += 1
        con.commit()
    finally:
        con.close()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--path", required=True)
    parser.add_argument("--max-chars", type=int, default=12000)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    root = Path(args.path).expanduser()
    observations = iter_observations(root, max_chars=args.max_chars)
    stats = import_observations(args.db, observations, dry_run=args.dry_run)
    print(json.dumps({
        "source_kind": SOURCE_KIND,
        "source_root": str(root),
        "observations_prepared": len(observations),
        **stats,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
