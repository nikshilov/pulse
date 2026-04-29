# 02 — Retrieve

Calls `POST /retrieve` with a query (and optional `UserState`) and prints
the ranked event IDs returned by Pulse's hybrid retrieval engine.

## What it does

1. Builds a `retrieveRequest` JSON body (`query`, `mode`, `top_k`,
   `user_state`).
2. Sends the request to a running Pulse server.
3. Prints the router decision (mode, classifier, confidence, reasoning)
   and the top-K event IDs.

The `mode` field accepts `auto` (default), `factual`, `empathic`, or
`chain`. With `auto`, the router classifier picks the mode and reports
its confidence.

## Run

```bash
# 1. Start a Pulse server with retrieval engine attached
make run                                       # default 127.0.0.1:18789

# 2. (Once) populate the graph — see example 01 + extraction pipeline,
#    or seed events directly via SQL.

# 3. Issue a query
PULSE_KEY=<secret-from-config> python3 run.py "как дела с Аней сегодня?"

# Or with a custom URL
PULSE_URL=http://localhost:18789 PULSE_KEY=... python3 run.py
```

## Expected output

```
POST /retrieve -> 200
  query:      'как дела с Аней сегодня?'
  mode_used:  empathic
  confidence: 0.82
  classifier: query-classifier-v1
  event_ids:  [42, 17, 105, 33, 9]
```

If the server returns `503 retrieval not configured`, the running Pulse
binary was built without a retrieval engine attached. Use `make build`
from a branch where `internal/retrieve` is wired into `cmd/pulse`.

## UserState — when conditional v3 boosts activate

`UserState` is optional. With no signals, retrieval falls back to the
v2_pure baseline (cosine + per-class decay). The example sends a
`mood_vector` with dominant `sadness=0.6` to illustrate the empathic-mode
boost; see [`internal/retrieve/state.go`](../../internal/retrieve/state.go)
for the full schema (sleep, HRV, HR trends, life events, etc.).

## Troubleshooting

- `connection failed` — server not running at `PULSE_URL`
- `401 unauthorized` — `PULSE_KEY` does not match `ipc_secret`
- `503 retrieval not configured` — server built without retrieval engine
