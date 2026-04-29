#!/usr/bin/env python3
"""03-end-to-end — ingest 5 events, then retrieve them by query.

This script chains examples 01 and 02 together and prints the full
pipeline output. It does NOT run the Sonnet/Opus extraction pipeline
(that requires ANTHROPIC_API_KEY and budget); instead it exercises the
ingest -> retrieve path directly. To run extraction afterwards, use
`scripts/pulse_extract.py --db ~/.pulse/pulse.db`.

Prerequisites:
    1. Pulse server running locally (see README)
    2. PULSE_KEY env var set
    3. (For retrieval) the server must have a retrieval engine wired in

Run:
    PULSE_KEY=... python3 run.py
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

PULSE_URL = os.environ.get("PULSE_URL", "http://127.0.0.1:18789")
PULSE_KEY = os.environ.get("PULSE_KEY")

SAMPLE_EVENTS = [
    ("e2e:1", "Сорвался после 10 лет трезвости — пиво в баре с другом.", 4),
    ("e2e:2", "Утренний звонок с мамой. Опять про работу. Я устал.", 3),
    ("e2e:3", "Подсел читать paper про memory retrieval — может поменять проект.", 2),
    ("e2e:4", "Аня сказала что хочет тишины сегодня. Не давил, ушёл в кабинет.", 1),
    ("e2e:5", "Поездка на мотоцикле, упал на повороте. Локоть ободран. Жив.", 0),
]


def post(path: str, body: dict, timeout: int = 30) -> dict:
    req = urllib.request.Request(
        f"{PULSE_URL}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Pulse-Key": PULSE_KEY,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def make_observation(source_id: str, text: str, days_ago: int) -> dict:
    captured = datetime.now(timezone.utc) - timedelta(days=days_ago)
    iso = captured.isoformat().replace("+00:00", "Z")
    return {
        "source_kind": "example",
        "source_id": source_id,
        "content_hash": "",
        "version": 1,
        "scope": "nik",
        "captured_at": iso,
        "observed_at": iso,
        "actors": [{"kind": "user", "id": "demo"}],
        "content_text": text,
    }


def step(num: int, label: str) -> None:
    print(f"\n=== STEP {num}: {label} ===")


def step_ingest() -> dict:
    step(1, "ingest 5 sample events")
    body = {"observations": [make_observation(sid, text, d)
                             for sid, text, d in SAMPLE_EVENTS]}
    resp = post("/ingest", body)
    print(f"  inserted={resp['inserted']}  duplicates={resp['duplicates']}  "
          f"revisions={resp['revisions']}")
    print(f"  ids={resp.get('ids', [])}")
    return resp


def step_consolidate_note() -> None:
    step(2, "consolidate (run separately)")
    print("  Consolidation runs as an offline job:")
    print("    python3 scripts/pulse_consolidate.py --db ~/.pulse/pulse.db")
    print("  Skipped here — requires the Python pipeline + OpenAI key.")


def step_retrieve(queries: list[str]) -> None:
    step(3, "retrieve")
    for q in queries:
        body = {"query": q, "mode": "auto", "top_k": 3}
        try:
            resp = post("/retrieve", body)
        except urllib.error.HTTPError as e:
            if e.code == 503:
                print(f"  query={q!r}")
                print(f"    503 retrieval not configured — server has no retrieval engine.")
                print(f"    TODO: rebuild the server with retrieval wired in.")
                continue
            raise
        print(f"  query={q!r}")
        print(f"    mode={resp.get('mode_used')}  conf={resp.get('confidence', 0):.2f}  "
              f"event_ids={resp.get('event_ids', [])}")


def main() -> int:
    if not PULSE_KEY:
        print("ERROR: set PULSE_KEY env var to your Pulse server's IPC secret",
              file=sys.stderr)
        return 2

    print(f"Pulse URL: {PULSE_URL}")
    try:
        step_ingest()
        step_consolidate_note()
        step_retrieve([
            "что-то про срыв и алкоголь",
            "как дела с Аней",
            "опасные ситуации с телом",
        ])
    except urllib.error.HTTPError as e:
        print(f"\nHTTP {e.code}: {e.read().decode('utf-8', errors='replace')}",
              file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"\nconnection failed: {e}", file=sys.stderr)
        print(f"is the server running at {PULSE_URL}?", file=sys.stderr)
        return 1

    print("\n=== DONE ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
