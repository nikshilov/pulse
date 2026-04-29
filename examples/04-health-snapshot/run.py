#!/usr/bin/env python3
"""04-health-snapshot — fetch Apple Health mock snapshots from Pulse.

Default returns today only; pass an integer to get N days of trend
(today first, oldest last). M0 returns fixture data (source="mock") so
demos work without the Mac→VDS Apple Health bridge.

Run:
    PULSE_KEY=... python3 run.py            # today
    PULSE_KEY=... python3 run.py 3          # last 3 days

Expected:
    GET /health/snapshot?days=3 -> 200
    [today] HRV=35 sleep=4.0h stress=0.72 source=mock
    [-1d  ] HRV=38 sleep=4.2h stress=0.65 source=mock
    [-2d  ] HRV=50 sleep=6.1h stress=0.45 source=mock
"""

import json
import os
import sys
import urllib.error
import urllib.request

PULSE_URL = os.environ.get("PULSE_URL", "http://127.0.0.1:18789")
PULSE_KEY = os.environ.get("PULSE_KEY")


def fetch(days: int | None) -> list[dict] | dict:
    path = "/health/snapshot"
    if days is not None and days > 1:
        path += f"?days={days}"
    req = urllib.request.Request(
        f"{PULSE_URL}{path}",
        headers={"X-Pulse-Key": PULSE_KEY or ""},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fmt(snap: dict, label: str) -> str:
    return (
        f"[{label:5s}] HRV={snap['hrv']} "
        f"sleep={snap['sleep_hours_last']}h "
        f"stress={snap['stress_proxy']:.2f} "
        f"source={snap['source']}"
    )


def main() -> int:
    if not PULSE_KEY:
        print("ERROR: set PULSE_KEY env var", file=sys.stderr)
        return 2

    days = None
    if len(sys.argv) > 1:
        try:
            days = int(sys.argv[1])
        except ValueError:
            print(f"bad days arg: {sys.argv[1]}", file=sys.stderr)
            return 2

    try:
        result = fetch(days)
    except urllib.error.HTTPError as e:
        print(f"GET /health/snapshot -> {e.code}", file=sys.stderr)
        print(e.read().decode("utf-8", errors="replace"), file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"connection failed: {e}", file=sys.stderr)
        print(f"is the server running at {PULSE_URL}?", file=sys.stderr)
        return 1

    print(f"GET /health/snapshot?days={days or 1} -> 200")
    if isinstance(result, dict):
        print(fmt(result, "today"))
    else:
        labels = ["today", "-1d", "-2d", "-3d", "-4d", "-5d", "-6d"]
        for i, snap in enumerate(result):
            print(fmt(snap, labels[i] if i < len(labels) else f"-{i}d"))

    return 0


if __name__ == "__main__":
    sys.exit(main())
