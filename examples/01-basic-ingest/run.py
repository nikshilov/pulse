#!/usr/bin/env python3
"""01-basic-ingest — POST 5 events into a running Pulse server.

Prerequisites:
    1. Pulse server running locally (see README; `make run` listens on 18789)
    2. PULSE_KEY env var set to the server's IPC secret
       (look at $PULSE_DATA/config.json — `ipc_secret`)
    3. Optionally PULSE_URL env var (default http://127.0.0.1:18789)

Run:
    PULSE_KEY=... python3 run.py

Expected output:
    POST /ingest -> 200
    {"inserted": 5, "duplicates": 0, "revisions": 0, "ids": [1, 2, 3, 4, 5]}
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

PULSE_URL = os.environ.get("PULSE_URL", "http://127.0.0.1:18789")
PULSE_KEY = os.environ.get("PULSE_KEY")


def make_observation(source_id: str, text: str, days_ago: int) -> dict:
    """Build a minimal Observation matching internal/capture/types.go."""
    captured_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return {
        "source_kind": "example",
        "source_id": source_id,
        "content_hash": "",  # server fills via ComputeContentHash
        "version": 1,
        "scope": "nik",
        "captured_at": captured_at.isoformat().replace("+00:00", "Z"),
        "observed_at": captured_at.isoformat().replace("+00:00", "Z"),
        "actors": [{"kind": "user", "id": "demo"}],
        "content_text": text,
    }


SAMPLE_EVENTS = [
    "Сорвался после 10 лет трезвости — пиво в баре с другом. Вышло само.",
    "Утренний звонок с мамой. Она опять про работу. Я сказал что устал.",
    "Подсел читать paper про memory retrieval — поменяет мой проект.",
    "Аня сказала что сегодня хочет тишины. Я понял, не давил.",
    "Поездка на мотоцикле, упал на повороте. Ободрал локоть. Жив.",
]


def main() -> int:
    if not PULSE_KEY:
        print("ERROR: set PULSE_KEY env var to your Pulse server's IPC secret",
              file=sys.stderr)
        return 2

    body = {
        "observations": [
            make_observation(f"demo:{i + 1}", text, days_ago=i)
            for i, text in enumerate(SAMPLE_EVENTS)
        ]
    }

    req = urllib.request.Request(
        f"{PULSE_URL}/ingest",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Pulse-Key": PULSE_KEY,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"POST /ingest -> {resp.status}")
            print(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"POST /ingest -> {e.code}", file=sys.stderr)
        print(e.read().decode("utf-8", errors="replace"), file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"connection failed: {e}", file=sys.stderr)
        print(f"is the server running at {PULSE_URL}?", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
