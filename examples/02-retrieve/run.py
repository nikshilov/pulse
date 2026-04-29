#!/usr/bin/env python3
"""02-retrieve — query Pulse via POST /retrieve and print ranked event IDs.

Prerequisites:
    1. Pulse server running with retrieval engine configured
       (see internal/retrieve — the /retrieve route is registered only when
       the server has been built with a retrieval engine attached).
    2. A populated graph (run example 01 + extraction first, or seed manually).
    3. PULSE_KEY env var set.

Run:
    PULSE_KEY=... python3 run.py "как дела с Аней сегодня?"

Expected output:
    POST /retrieve -> 200
    mode_used:  empathic
    confidence: 0.82
    event_ids:  [42, 17, 105]
"""

import json
import os
import sys
import urllib.error
import urllib.request

PULSE_URL = os.environ.get("PULSE_URL", "http://127.0.0.1:18789")
PULSE_KEY = os.environ.get("PULSE_KEY")

DEFAULT_QUERY = "что важно для меня прямо сейчас"


def retrieve(query: str, mode: str = "auto", top_k: int = 5,
             user_state: dict | None = None) -> dict:
    body = {"query": query, "mode": mode, "top_k": top_k}
    if user_state is not None:
        body["user_state"] = user_state

    req = urllib.request.Request(
        f"{PULSE_URL}/retrieve",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Pulse-Key": PULSE_KEY,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    if not PULSE_KEY:
        print("ERROR: set PULSE_KEY env var to your Pulse server's IPC secret",
              file=sys.stderr)
        return 2

    query = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_QUERY

    # Optional: provide a UserState so v3 conditional boosts activate.
    user_state = {
        "mood_vector": {"sadness": 0.6, "anger": 0.2},
        # body signals omitted -> state boost stays neutral
    }

    try:
        result = retrieve(query, mode="auto", top_k=5, user_state=user_state)
    except urllib.error.HTTPError as e:
        print(f"POST /retrieve -> {e.code}", file=sys.stderr)
        print(e.read().decode("utf-8", errors="replace"), file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"connection failed: {e}", file=sys.stderr)
        print(f"is the server running at {PULSE_URL}?", file=sys.stderr)
        return 1

    print(f"POST /retrieve -> 200")
    print(f"  query:      {query!r}")
    print(f"  mode_used:  {result.get('mode_used')}")
    print(f"  confidence: {result.get('confidence'):.2f}")
    print(f"  classifier: {result.get('classifier')}")
    if result.get("reasoning"):
        print(f"  reasoning:  {result['reasoning']}")
    print(f"  event_ids:  {result.get('event_ids', [])}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
