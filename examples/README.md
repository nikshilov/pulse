# Pulse examples

Minimal, runnable scripts that exercise the Pulse HTTP API. Each example
is a single Python file (stdlib only — no extra deps) plus a README.

| # | Folder | What it does |
|---|---|---|
| 01 | [`01-basic-ingest/`](./01-basic-ingest/) | POST 5 sample events to `/ingest` |
| 02 | [`02-retrieve/`](./02-retrieve/) | Query `/retrieve` with optional `UserState` |
| 03 | [`03-end-to-end/`](./03-end-to-end/) | Ingest -> retrieve in one run |

## Common prerequisites

```bash
# 1. Build + run the server (from repo root)
make run                                # default 127.0.0.1:18789

# 2. Find the IPC secret (auto-generated on first run)
cat ~/.pulse/config.json   # field: ipc_secret

# 3. Export it for examples
export PULSE_KEY=<secret>
export PULSE_URL=http://127.0.0.1:18789  # optional, this is the default
```

All examples honour `PULSE_KEY` (required) and `PULSE_URL` (optional).
They use only `urllib` from the standard library, so no `pip install`
is needed.

## Why Python

The Pulse server is Go, but most users will integrate via HTTP from
Python (LLM tooling, notebooks, ingest pipelines). The examples mirror
how `scripts/pulse_ingest.py` and downstream consumers talk to the
server.

If you want a Go example, the Go test suite under
`internal/ingest/` and `internal/server/` exercises the same handlers
in-process — that is the canonical Go reference.
