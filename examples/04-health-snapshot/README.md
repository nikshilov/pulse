# 04 — Health Snapshot (mock)

Calls `GET /health/snapshot` and prints today's Apple Health snapshot
(or a multi-day trend with `?days=N`).

## What it does

1. Issues a GET to `/health/snapshot` (default) or
   `/health/snapshot?days=N` for a trend.
2. Prints HRV / sleep / stress per day.

In M0 the response is **fixture-only** (`source="mock"`). The real
Mac → VDS Apple Health bridge is unrelated infrastructure; the mock
endpoint exists so demos and the Hearth chat client can develop
against a stable shape today.

## Run

```bash
# 1. Start the Pulse server (data-dir auto-creates secret + sqlite)
make run                                       # default 127.0.0.1:18789

# 2. Today
PULSE_KEY=<secret> python3 run.py

# 3. Last 3 days (trend)
PULSE_KEY=<secret> python3 run.py 3
```

## Expected output

```
GET /health/snapshot?days=3 -> 200
[today] HRV=35 sleep=4.0h stress=0.72 source=mock
[-1d  ] HRV=38 sleep=4.2h stress=0.65 source=mock
[-2d  ] HRV=50 sleep=6.1h stress=0.45 source=mock
```

## Schema

```json
{
  "hrv": 35,
  "stress_proxy": 0.72,
  "sleep_quality": 0.40,
  "sleep_hours_last": 4.0,
  "sleep_hours_avg_7d": 5.3,
  "steps_today": 1850,
  "last_workout_days": 4,
  "hr_trend": "elevated_overnight",
  "hrv_trend": "declining_3d",
  "timestamp": "2026-04-29T17:30:00Z",
  "source": "mock"
}
```

Field names are aligned with the `UserState` shape in
[`hearth/chat/src/api.ts`](../../../hearth/chat/src/api.ts) and
[`internal/retrieve/state.go`](../../internal/retrieve/state.go) so
callers can pluck values straight into `/retrieve`'s `user_state`
parameter.

## Replacing the mock

When the real Apple Health bridge lands, swap the
`health.NewFixtureProvider(...)` wiring in `cmd/pulse/main.go` for a
provider that reads `/home/openclaw/persistent/elle-health.db` (or
the equivalent) — handler stays unchanged.

## Troubleshooting

- `connection failed` — server not running at `PULSE_URL`
- `401 unauthorized` — `PULSE_KEY` does not match `ipc_secret`
- `503 health provider not configured` — server started without a
  Health provider (shouldn't happen in default `cmd/pulse`)
