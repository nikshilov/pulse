"""claude-jsonl provider — scan ~/.claude/projects/*.jsonl → Observations."""

import hashlib
import httpx
import json
import sys
from pathlib import Path
from typing import Iterator, Optional

SKIP_SYSTEM_PREFIXES = ("<system-reminder", "<command-name>", "<local-command")


def _agent_id_from_cwd(cwd: str | None) -> str:
    if not cwd:
        return "unknown"
    return Path(cwd).name or "unknown"


def _content_hash(content: str, metadata: dict) -> str:
    h = hashlib.sha256()
    h.update(content.encode("utf-8"))
    h.update(b"\x1f")
    if metadata:
        canonical = [[k, metadata[k]] for k in sorted(metadata.keys())]
        h.update(json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return h.hexdigest()


def _extract_text(content) -> Optional[str]:
    """Pull text content from user/assistant message. Skip tool_use, tool_result, thinking."""
    if isinstance(content, str):
        text = content.strip()
        return text or None
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                t = (block.get("text") or "").strip()
                if t:
                    parts.append(t)
            # skip tool_use, tool_result, thinking
        return "\n".join(parts) if parts else None
    return None


def _skippable_system_xml(text: str) -> bool:
    return any(text.lstrip().startswith(p) for p in SKIP_SYSTEM_PREFIXES)


def normalize_line(line: str, source_file: str, line_index: int) -> Optional[dict]:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None

    if obj.get("isMeta"):
        return None

    msg_type = obj.get("type")
    if msg_type not in ("user", "assistant"):
        return None

    message = obj.get("message") or {}
    text = _extract_text(message.get("content"))
    if not text or _skippable_system_xml(text):
        return None

    ts = obj.get("timestamp") or obj.get("createdAt")
    if not ts:
        return None

    cwd = obj.get("cwd")
    agent_id = _agent_id_from_cwd(cwd)
    if msg_type == "user":
        actor_primary = {"kind": "user", "id": "nik"}
    else:
        actor_primary = {"kind": "assistant", "id": agent_id}

    metadata = {
        "session_id": obj.get("sessionId"),
        "cwd": cwd,
        "git_branch": obj.get("gitBranch"),
        "model": (message.get("model") if isinstance(message, dict) else None),
    }
    metadata = {k: v for k, v in metadata.items() if v is not None}

    return {
        "source_kind": "claude_jsonl",
        "source_id": f"{source_file}:{line_index}",
        "content_hash": _content_hash(text, metadata),
        "version": 1,
        "scope": "shared",
        "captured_at": ts,
        "observed_at": ts,
        "actors": [actor_primary],
        "content_text": text,
        "metadata": metadata,
    }


def scan_file(path: Path) -> Iterator[dict]:
    with path.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            obs = normalize_line(line, source_file=path.name, line_index=i)
            if obs:
                yield obs


def post_batch(pulse_url: str, observations: list[dict]) -> dict:
    resp = httpx.post(
        f"{pulse_url}/ingest",
        json={"observations": observations},
        timeout=60.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"ingest failed {resp.status_code}: {resp.text}")
    return resp.json()


def run(args) -> int:
    base = Path(args.path).expanduser()
    if not base.exists():
        print(f"path not found: {base}", file=sys.stderr)
        return 2

    files = sorted(base.rglob("*.jsonl")) if base.is_dir() else [base]
    print(f"scanning {len(files)} files under {base}")

    batch: list[dict] = []
    total_inserted = total_dup = total_rev = 0
    for f in files:
        for obs in scan_file(f):
            batch.append(obs)
            if len(batch) >= args.batch_size:
                if not args.dry_run:
                    stats = post_batch(args.pulse_url, batch)
                    total_inserted += stats.get("inserted", 0)
                    total_dup += stats.get("duplicates", 0)
                    total_rev += stats.get("revisions", 0)
                batch.clear()

    if batch and not args.dry_run:
        stats = post_batch(args.pulse_url, batch)
        total_inserted += stats.get("inserted", 0)
        total_dup += stats.get("duplicates", 0)
        total_rev += stats.get("revisions", 0)

    print(f"DONE: inserted={total_inserted} duplicates={total_dup} revisions={total_rev}")
    return 0
