# pulse/cartographer/

Phase K of Pulse — the **Cartographer**: a continuously-updating, scientifically-grounded user-profile schema with onboarding mode, post-turn extraction, curiosity engine, and map visualization.

## Files

| File | What |
|---|---|
| [`SPEC.md`](SPEC.md) | Full design spec — vision, schema, two modes, curiosity, visualization, pipeline, timeline |
| [`schema.yaml`](schema.yaml) | Canonical machine-readable user-profile schema (v1.0.0). Single source of truth for Go structs, DB migration, and visualization rendering. |
| [`prompts/onboarding_ru.md`](prompts/onboarding_ru.md) | Cartographer onboarding prompt, Russian (ported from Garden v3, validated on real user) |
| [`prompts/onboarding_en.md`](prompts/onboarding_en.md) | Cartographer onboarding prompt, English (XML-formatted v4) |
| [`prompts/continuous.md`](prompts/continuous.md) | Post-turn extractor — runs after each user-companion exchange, patches profile JSON |
| [`prompts/curiosity.md`](prompts/curiosity.md) | Gap analyzer — produces 3-5 ranked threads the Cartographer wants to lean into next |
| [`examples/nik_v3_profile.json`](examples/nik_v3_profile.json) | Reference output JSON from a real cartographer session (Nik Shilov, March 2026). Use as fixture for tests. |

## Status

**Phase K.0 (this commit) — documentation only.** No code yet.

Implementation phases (in `SPEC.md`):
- K.1: migration `018_user_profile.sql` + Go struct types
- K.2: continuous extractor (Go + Claude Haiku) post `/ingest` hook
- K.3: onboarding mode in webapp (`?onboarding=1` flow)
- K.4: curiosity engine + system-prompt bias in `web/src/llm.ts`
- K.5: `<profile-map>` web component (mosaic + chips + animations)
- K.6: API endpoints `GET /profile`, `POST /profile/refresh-curiosity`
- K.7: demo seed — replay a real cartographer chat, watch profile rebuild itself

Total ~25h focused work after K.0.

## Why this is canon, not a sketch

The schema, the onboarding prompt, the methodology stack (MI / PACS / DCM / ASMR / self-distancing) are the result of **Deep Research + 2 user tests on a real person** that ran in March 2026. The reference JSON in `examples/` is from that user. **We do not redesign these. We port and extend.**

Phase K extends the canon with three additions the original did not have:

1. **Continuous mode** — schema fills not just from the onboarding session but from every subsequent conversation
2. **Curiosity engine** — explicit gap analysis that biases the companion's system prompt without forcing questionnaires
3. **Map visualization** — the user can see their map filling, which is itself a delight-loop that keeps people engaged

## Scientific provenance

| Component | Source |
|---|---|
| OARS framework, 2:1 reflection ratio, 0-10 rulers | Motivational Interviewing — Magill et al. (continuously updated meta-analyses) |
| Discourse-based attachment inference | PACS — Talia, A., Miller-Bottome, M., & Daniel, S. I. F. (2017) |
| SES / SIS1 / SIS2 erotic profile | Dual Control Model — Janssen, E., & Bancroft, J. (2007) |
| ASMR responsivity assessment | Poerio, G. L. et al. (2018) |
| Self-distancing language for trauma | Murdoch et al. |
| Crisis protocol (stay in conversation, not hotline) | NIST AI RMF + Garden internal standard |

## License

Same as parent (`pulse/` repo) — MIT.
