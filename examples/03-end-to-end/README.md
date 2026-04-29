# 03 — End-to-end demo

Chains example 01 (ingest) and example 02 (retrieve) into one run.
Designed to be the answer to "what does Pulse actually do?"

## What it does

```
1. POST /ingest      x5 observations
2. (skipped)          consolidate runs offline — instructions printed
3. POST /retrieve    x3 queries, prints mode_used + ranked event_ids
```

Step 2 is intentionally skipped: consolidation requires the Python
pipeline + OpenAI key + extraction budget. Run it manually after the
demo if desired:

```bash
python3 scripts/pulse_extract.py --db ~/.pulse/pulse.db
python3 scripts/pulse_consolidate.py --db ~/.pulse/pulse.db
```

## Run

```bash
# 1. Start the server (in another terminal)
make run                                # default 127.0.0.1:18789

# 2. Run the demo
PULSE_KEY=<secret-from-config> make demo
# or directly
PULSE_KEY=... python3 examples/03-end-to-end/run.py
```

## Expected output

```
Pulse URL: http://127.0.0.1:18789

=== STEP 1: ingest 5 sample events ===
  inserted=5  duplicates=0  revisions=0
  ids=[1, 2, 3, 4, 5]

=== STEP 2: consolidate (run separately) ===
  Consolidation runs as an offline job:
    python3 scripts/pulse_consolidate.py --db ~/.pulse/pulse.db
  Skipped here — requires the Python pipeline + OpenAI key.

=== STEP 3: retrieve ===
  query='что-то про срыв и алкоголь'
    mode=empathic  conf=0.78  event_ids=[1, 5]
  query='как дела с Аней'
    mode=empathic  conf=0.81  event_ids=[4, 2]
  query='опасные ситуации с телом'
    mode=empathic  conf=0.66  event_ids=[5, 1]

=== DONE ===
```

If the server has no retrieval engine attached, step 3 prints:

```
  query='...'
    503 retrieval not configured — server has no retrieval engine.
```

Step 1 (ingest) still works in that case.

## What this is NOT

- A benchmark — see `~/dev/ai/bench/` for the empathic memory bench
- An extraction demo — see `scripts/pulse_extract.py`
- A production deploy — see `CONTRIBUTING.md`
