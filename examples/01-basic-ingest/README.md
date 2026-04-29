# 01 — Basic ingest

Posts 5 sample events into a running Pulse server via `POST /ingest`.

## What it does

Builds 5 minimal `Observation` payloads (one per day of the last 5 days),
sends them in a single batched request, and prints the server response
(insert / duplicate / revision counts).

The payload shape matches `internal/capture/types.go`. Pulse's ingest
handler (`internal/ingest/handler.go`) computes the content hash and
upserts each row, enqueueing an extraction job per accepted observation.

## Run

```bash
# 1. Start a local Pulse server (in another terminal)
make run                  # from repo root, default 127.0.0.1:18789

# 2. Find the IPC secret (auto-generated on first run)
cat ~/.pulse/config.json   # field: ipc_secret

# 3. Post the events
PULSE_KEY=<secret-from-config> python3 run.py
```

## Expected output

```
POST /ingest -> 200
{"inserted":5,"duplicates":0,"revisions":0,"ids":[1,2,3,4,5]}
```

A second run prints `"duplicates":5` because the content hashes match.

## Customizing

Edit `SAMPLE_EVENTS` in `run.py`. Each string becomes one observation.
For other source kinds (Telegram, Claude JSONL, Gmail) see
`scripts/providers/` and `scripts/pulse_ingest.py`.

## Troubleshooting

- `connection failed` — check the server is running and `PULSE_URL` matches
- `401 unauthorized` — `PULSE_KEY` does not match `ipc_secret`
- `400 bad request` — payload validation failed (check the error body for
  the field name and observation index)
