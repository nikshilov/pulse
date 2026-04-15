#!/usr/bin/env python3
"""pulse_ingest — batch-import observations into Pulse from local sources."""

import argparse
import sys

KNOWN_SOURCES = ["claude-jsonl"]


def main() -> int:
    p = argparse.ArgumentParser(
        prog="pulse_ingest",
        description="Batch-import observations into Pulse.",
    )
    p.add_argument(
        "--source",
        required=True,
        choices=KNOWN_SOURCES,
        help="which provider adapter to use (e.g. claude-jsonl)",
    )
    p.add_argument("--path", required=True, help="source filesystem path")
    p.add_argument("--since", help="ISO date floor (YYYY-MM-DD)", default=None)
    p.add_argument(
        "--pulse-url",
        default="http://localhost:18789",
        help="Pulse server base URL",
    )
    p.add_argument("--batch-size", type=int, default=200)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if args.source == "claude-jsonl":
        from providers.claude_jsonl import run as run_claude_jsonl
        return run_claude_jsonl(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
