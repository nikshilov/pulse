# Pulse Lab

> State-aware retrieval — flip a slider, see retrieval flip.

A zero-config browser playground showing what makes Pulse Pulse: **the same query returns different events depending on user state.**

Not a chat. Not a companion. Just the engine, naked, inspectable.

## What it shows

Three panes:

| Pane | What |
|---|---|
| **Corpus** (left) | 50 fixture events with emotion tags, valence, anchors. Click an event → it becomes the query. Events that appeared in the last retrieval glow. |
| **Retrieve** (center) | Query input. Top-5 ranked events with full score breakdown: `topic`, `emotion`, `state`, `recency`, `anchor`. Why-string explains each result. |
| **State** (right) | Plutchik-10 mood sliders + biometric (HRV, stress, sleep). Move any slider → retrieval re-ranks instantly. |

The header has `randomize state` and `reset` buttons for the demo loop.

## The viral 30-second clip

1. Land on Lab — neutral state, query: *"когда я чувствовал что меня видят целиком"*
   → top results: balanced anchors (mama-уверен, intimacy moment, deceased grandfather)
2. Slide `fear` → 0.85, `shame` → 0.55, `hrv` → 38
   → top results flip to: **shame-core anchors** (kindergarten box, father chained himself, said no to mother) — score on #40 jumps 0.178 → 0.728 (4×)
3. Reset → balance returns

## Quickstart

```bash
cd lab/
npm install
npm run dev   # http://localhost:5174
```

No backend, no API keys, no Pulse engine running. Pure browser demo.

## How is this different from real Pulse?

| | Lab (this) | Real Pulse engine |
|---|---|---|
| Storage | 50 fixture events in JS | SQLite + 3072d embeddings |
| Vector retrieval | Keyword overlap | Cosine on text-embedding-3-large |
| Emotion classifier | Hand-coded fixture | Plutchik-10 LLM tagger (Qwen) |
| State boost | Simplified rules | Full v3 conditional model |
| Persistence | None | Per-event ingest + decay |
| Where it runs | Browser | Go HTTP server |

The Lab simulator preserves the **shape** of v3 retrieval — `weighted score = α·topic + β·emotion + γ·state + δ·recency + ε·anchor` — but cuts every dependency so it works anywhere.

For real retrieval against your own corpus → use [Hearth](https://github.com/nikshilov/hearth) (chat client) or the engine HTTP API directly.

## What's intentionally NOT here

- No LLM (this would dilute the demo of the *engine*)
- No chat UI (that's [Hearth](https://github.com/nikshilov/hearth))
- No auth, no signup, no telemetry
- No animations beyond the retrieval re-rank itself
- No mobile responsive (desktop demo first; iOS later via WKWebView)

## License

MIT — see parent [LICENSE](../LICENSE).
