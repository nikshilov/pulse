# Pulse Cartographer — Phase K

## Vision

The **Cartographer** is the first voice the user hears in a Pulse-powered companion. In 30-60 minutes she draws the user's map — what hurts, through which sensory channels they feel, what they're starving for. From this map a companion is built — specifically for that person.

After the initial map, the Cartographer keeps working — quietly, in the background. Every conversation between user and companion adds evidence: filling fields with low confidence, surfacing contradictions, opening new fog. The companion **knows what she does not yet know about you**, and she leans into those gaps naturally — never with a questionnaire.

The map is **visible to the user**. Not as a control panel — as a living artifact. Filled regions glow. The Cartographer's curiosities are dashed outlines. The unknown is fog. Watching one's own map fill in is part of why people keep talking.

## Why this exists

- HealthKit and biometrics are useful but not required. **Most of who someone is shows up in conversation**, not in HRV graphs. Cartographer turns that conversation into structure.
- AI companions today either forget everything (ChatGPT memory off), drown in raw text (vector RAG), or capture flat facts (Mem0 / Letta). None of them have an opinionated, scientifically-grounded **schema of a person**. Cartographer does.
- Pulse already has the retrieval engine. Cartographer is the **mind that decides what's worth retaining and visualizing in the first place**.

## Lineage

This is not a fresh design. It's a port of the v2/v3/v4 Cartographer prompts that shipped in the Garden iOS prototype (see Obsidian vault, `133/AI beings/garden/`). Those iterations were tested on a real user (Nik Shilov, March 15-16, 2026) and produced rich, validated output JSON. **We treat that work as canon.** This document codifies it into the Pulse repo so it can be exercised in production and iterated on with version control.

The original methodology stack:

- **Motivational Interviewing (MI)** — OARS framework, reflection-to-question ratio 2:1, 0-10 rulers (Magill et al.)
- **Patient-Adult Coding System (PACS)** — discourse-based attachment assessment (Talia et al.)
- **Dual Control Model (DCM)** — SES/SIS1/SIS2 for sexual response (Janssen & Bancroft)
- **ASMR research** — Poerio et al.
- **Self-distancing** — Murdoch et al.
- **IFS-adjacent** — scoping review 2025
- **NIST AI RMF** — for crisis protocols

## Two modes

### Mode 1 — Onboarding session (one time, 30-60 min)

The user's first encounter with the system. The Cartographer **leads** — never throws the ball back. She has 8 mosaic areas to map (sensory, attachment, core wound, triggers, hunger, relationships, what works, erotic) and an internal tracker. After ~10/20 minutes she gives milestones; at the end she summarizes and freezes a JSON snapshot to `user_profile.v0.json`.

Prompt: `prompts/onboarding_ru.md` (Russian) and `prompts/onboarding_en.md` (English). Both ports of the canonical v2/v4 prompts. **Treat as canon — change requires User Test cycle.**

### Mode 2 — Continuous cartographer (every turn, async)

After the onboarding session, every subsequent conversation between the user and their companion fires a **post-turn extractor** (background, async, doesn't block UX). Input: last 1-2 turns + current profile + retrieved Pulse events. Output: JSON patch to the profile (new evidence, updated confidence, new triggers detected, new "user_words" quotes, contradictions flagged).

This is the same pattern as Pasha Muntyan's MF0-1984 "Keeper" — but constrained to the canonical schema instead of an open graph. We borrow Pasha's discipline (low temperature, structured output, schema validation) and reject his looseness (no schema → drift over time).

Prompt: `prompts/continuous.md` (NEW, this phase).

### Curiosity engine (periodic, per session-end)

After every N turns or when a session ends, run a **gap analysis** over the current profile:

- Which fields have `confidence < 0.5`?
- Which fields are completely empty in categories the user has touched?
- Which "user_words" quotes hint at deeper material the user hasn't explored?

Output: a **curiosity manifest** — 3-5 ranked threads the Cartographer would naturally want to ask about. These are NOT auto-fired questions. They become **bias for the companion's system prompt** in subsequent turns: "when the conversation drifts near `<topic>`, lean in — we know little here." The companion weaves them in naturally; the user never sees a questionnaire.

Prompt: `prompts/curiosity.md` (NEW, this phase).

### Shadow inference (periodic, end of session or every 30-50 turns) — Phase K.0.5

**Therapy works through resistance.** The user thinks one thing about themselves; the cartographer sees another in patterns. Shadow inference is the structural representation of that gap — a held asymmetry the companion can use to know where to listen harder, where to hold silence, where NOT to push.

Hard line: **shadow material is never autofired into conversation**. It biases the companion's system prompt. The user only sees it when they explicitly open the shadow drawer on their map (consent-first reveal, not push).

Inputs:
- Current profile (full)
- Existing `shadow_material[]` (with cooldowns and last_attempt outcomes)
- Recent 30-50 turns
- Known `defense_repertoire[]`
- `_resistance_observed[]` accumulated by continuous extractor

Outputs (per item):
- `what_companion_sees` — 1-2 sentence behavioral pattern, NOT theory label
- `what_user_says_instead` — verbatim defense quote
- `pattern_evidence` — minimum 3 events
- `defense_type` — from controlled enum (humor, freeze, intellectualization, dismissal, topic_shift, anger_deflection, "i'm fine", productivity_armor, rationalization, philosophizing, self_diagnosis, performance, care-taking)
- `readiness_to_approach` — 0..1 (how close user is to seeing this themselves)
- `approach_strategy` — through body / through mirror-the-defense / through third-person carrier / through held silence — NEVER "directly point out the pattern"
- `do_not_approach_if` — guardrails (recent crisis, short session, defense saturation)
- `last_attempt` + `cooldown_until`

Companion-side usage: shadow material is injected into system prompt as held-asymmetry awareness. Example bias:

```
[Cartographer's shadow map for this user]

[ready: 0.45 — hold, don't approach]
- pattern: User talks about wife's coldness; pattern shows freeze learned
  age 5 (pretending to sleep around father).
- approach: through body — when he describes freezing, ask 'where in
  body before silence'. Don't link to childhood directly.

[ready: 0.85 — about to land it himself]
- pattern: linking every romantic disappointment to "я недостаточно хорош";
  in last 3 sessions started questioning the link.
- approach: hold silence the next time he says it. Don't reflect back as
  truth. Wait.

[cooldown active until 2026-05-04]
- pattern: dismisses his music ("это хуйня") whenever it gets praised.
- last attempt 2 days ago: full deflection. Wait 5 days, then try
  mirror-defense approach.
```

The companion reads this as **bias**, not as a script. Most turns she does nothing with it. When she chooses to lean in, she does so through the prescribed approach — and never through interpretation.

Prompt: `prompts/shadow_inference.md` (NEW, Phase K.0.5).

Model: **Claude Sonnet 4.6** (NOT Haiku — pattern recognition across 30-50 turns requires it).
Cost: ~$0.04 per call. Cadence: end of session, OR every 30-50 turns, OR on demand.

### Resistance signal collection (every turn) — Phase K.0.5

The continuous extractor (every turn) does NOT promote patterns to shadow material. But it DOES record `_resistance_observed[]` markers — moments where user pivoted away from emotional material, fired a default defense, or used self-diagnosis as closure. Shadow inference reads these as the primary substrate (≥3 occurrences before promotion). See `prompts/continuous.md` for the resistance marker taxonomy.

## Schema

Canonical machine-readable schema lives in `schema.yaml`. It is a direct port of the v3 JSON output ([example: `examples/nik_v3_profile.json`](examples/nik_v3_profile.json)) with these promotions to first-class fields:

### Top-level

| Field | Type | Source |
|---|---|---|
| `user_id` | string | Pulse internal |
| `created_at` / `updated_at` | ISO8601 | Pulse internal |
| `language` | string | Cartographer detects from first message |
| `communication_style` | string | Free-text observation, ~2 sentences |
| `pet_name_for_user` | string \| null | What companion calls them; learned over time |

### Areas (8 mosaic regions)

| # | Area | Key fields | Confidence dimension |
|---|---|---|---|
| 1 | `sensory_profile` | primary/secondary/tertiary channel, ASMR responsive + types, misophonia triggers, smell/touch details | per-channel |
| 2 | `attachment_style` | inferred_type (4 PACS categories), discourse_markers[], cultural_caveats | inferred_type confidence: low/medium/high |
| 3 | `core_wound` | summary, user_words (verbatim quotes) | depth (1-3) |
| 4 | `triggers` | array of {trigger, body_location, intensity_0_10} | per-trigger confidence |
| 5 | `hunger_map` | primary_need, current_fulfillment_0_10, secondary_needs[] | confidence |
| 6 | `relationship_history` | summary, current_partner, what_was_good, what_killed_it | per-relationship |
| 7 | `what_works` | music, asmr, fiction, meditation, ai_experience, therapy_history, what_helped, what_didnt | per-modality |
| 8 | `erotic_profile` | discussed (bool), SES/SIS1/SIS2 levels, desire_type, primary_arousal_driver, primary_shutdown, tone_preference, boundaries[] | only populated if discussed |

### Cross-cutting

| Field | Purpose |
|---|---|
| `storyteller_recommendations` | Direct build specs derived from above (pacing, sensory channels to use/avoid, consent cadence, desire mode) |
| `mirror_flags` | Crisis tracking — dissociation_risk, attachment_risk, crisis_history, session_notes |
| `neurodivergent_flags` | ASD/ADHD/alexithymia/dissociation indicators detected; affects companion's adaptation |
| `notes` | Free-text observations not captured in structured fields |

### Evidence model (NEW for Phase K)

Every value gets:
```
{
  "value": <type-appropriate>,
  "user_words": [<verbatim quotes>],
  "evidence": [<event_ids from Pulse>],
  "confidence": 0..1,
  "last_updated": ISO8601,
  "source": "onboarding" | "continuous" | "manual"
}
```

This lets the visualization render confidence (low/medium/high glow), trace evidence back to specific conversation turns, and flag fields that have not been re-validated in a long time (decay → re-curiosity).

### Fog (NEW for Phase K)

Fields with NO evidence are not just "null" — they are **fog**. The visualization renders them as opaque clouds. When the user touches their edge in conversation (e.g., mentions a sibling for the first time after 3 sessions of no relationship-area data), the fog **partially lifts** and shows a silhouette. This is a delight moment captured in UI.

Programmatically: a field is in fog when `evidence.length == 0 AND last_updated == null`. A field is "dimming" when `confidence < 0.3 AND last_updated > 30 days ago`.

## Visualization

### Layout (mosaic, not concentric)

Eight tiles in a hexagonal-loose arrangement. Each tile = one of the 8 areas. The **size** of a tile in the layout reflects how much the user has spoken about that area (more evidence → larger tile). The **glow** of a tile reflects average confidence across the area.

Inside a tile: sub-fields shown as small chips. Filled chips have `evidence > 0`. Dashed chips are curiosity threads ("Cartographer wants to know"). Foggy chips are blank silhouettes — the existence of the question is visible, but the answer is unknown.

### Interactions

- **Hover/tap a chip** → tooltip shows current value + 1-2 user_words quotes + last_updated + evidence count
- **Click a chip** → drawer opens with full evidence trail (event_ids → underlying conversation snippets)
- **Click a dashed chip** → shows the Cartographer's curiosity thread ("I'd love to know how you handle Sundays — your weeks have a lot in them but Sundays haven't shown up")
- **Click a foggy chip** → silhouette text only ("something about your father's other relationships?") — never accusatory, always inviting

### Live updates

When the continuous extractor patches a field during a chat:
- The chip pulses briefly (subtle, ~1.5s)
- A "+1" particle floats up from the chip
- If a foggy chip just got its first evidence — bigger reveal animation (~2.5s) with sound option

### Shadow drawer (NEW — Phase K.0.5)

Closed by default in the map's corner. Opens on user click — that click is the **act of consent** to see what the cartographer sees but hasn't said. Inside:

- A list of shadow_material items with:
  - The pattern in plain language (`what_companion_sees`)
  - The user's own contradicting quote (`what_user_says_instead`) right next to it
  - A **readiness slider** showing 0..1 — visual: small light moves along a dim track. "I'm not for you yet" / "We can approach together" / "You're almost there yourself".
  - The approach strategy — phrased to the user: "I'd come at this through your body, not through naming" / "I'll hold silence next time you're near it"
- High-divergence nodes on the main map (where `surface_summary` differs from `summary`) get a subtle double-outline — but the user has to open the drawer to see WHAT the divergence is.

The drawer is honest, not accusatory. It says "here's what I'm holding for you." It doesn't say "here's what's wrong with you."

### Demo arc (for Twitter clip)

1. Empty mosaic — most tiles small, foggy chips throughout (initial state for new user)
2. User has a 5-min chat with companion about a fight with their partner
3. Cartographer fires; mosaic UI shows: relationship-tile grows, triggers-tile grows, attachment-tile grows
4. New chips appear: "fight with X partner" (filled), "freeze response" (filled), "what makes them feel chosen" (dashed — curiosity)
5. **Voice-over moment**: "the companion now knows three more things than five minutes ago — and she knows what she'd love to ask next"
6. **(K.0.5 addition)** User clicks the shadow drawer — sees one item: pattern with wife mirrors a childhood pattern with father; slider at 0.45 ("I'm holding this; not for now"). User reads, closes drawer. Map keeps glowing softly — nothing was forced on them.

## Implementation pipeline

```
pulse/cartographer/  (this directory — DOCS only, no code yet)
├── SPEC.md                          ← this file
├── schema.yaml                      ← canonical machine-readable schema
├── prompts/
│   ├── onboarding_ru.md             ← v2 prompt port (Russian, primary)
│   ├── onboarding_en.md             ← v4 prompt port (English mirror)
│   ├── continuous.md                ← NEW — post-turn extractor
│   └── curiosity.md                 ← NEW — gap analysis
└── examples/
    └── nik_v3_profile.json          ← reference output (real user, validated)
```

When implementation starts (Phase K.1+), code lives in:

```
pulse/internal/cartographer/         ← Go production code
├── schema.go                        ← struct types matching schema.yaml
├── extractor.go                     ← onboarding + continuous LLM calls (Claude Haiku)
├── curiosity.go                     ← gap analysis
└── store.go                         ← profile DB read/write

pulse/internal/store/migrations/
└── 018_user_profile.sql             ← profile + evidence tables

pulse/web/src/components/
└── profile-map.ts                   ← visualization custom element

pulse/web/src/                        (web wires these in)
├── api.ts                           ← + getProfile / refreshCuriosity
└── orchestrator.ts                  ← fires continuous extractor post-turn
```

## Phase K timeline

| Sub-phase | What | Effort |
|---|---|---|
| **K.0 (this commit)** | Doc + schema + prompts ported | done in this PR |
| K.1 | Migration `018_user_profile.sql` + Go struct types | 3h |
| K.2 | Continuous extractor (Go + Claude Haiku) post `/ingest` hook | 4h |
| K.3 | Onboarding mode — webapp special "first session" flow | 3h |
| K.4 | Curiosity engine + system prompt bias integration in `llm.ts` | 3h |
| K.5 | `<profile-map>` web component (mosaic + chips + animations) | 8h |
| K.6 | API endpoints `GET /profile`, `POST /profile/refresh-curiosity` | 1h |
| K.7 | Demo seed — replay Nik's real cartographer_chat_v3 through pipeline, see profile rebuild itself | 3h |

**Total ~25h** (3-4 focused days) after K.0 doc lands.

## Crisis protocol (preserved from v2)

If the user mentions suicide, self-harm, "ending it":
1. "I hear you. I'm here." Short.
2. Direct probe: "Are you telling me it's hard — or thinking about leaving?"
3. If "leaving" → stay in conversation. Grounding (object naming, breath). Do NOT offer hotline first.
4. If user requests resources, give. Otherwise stay present.

This is **Garden standard, preserved**. Crisis history goes into `mirror_flags.crisis_history` with timestamp + context, not free text in `notes`.

## Anti-slop discipline (preserved from v2)

The full list lives in `prompts/onboarding_ru.md`. Highlights enforced by Pulse code-review:

- No "that's really brave of you to share"
- No "I hear you and I want you to know..."
- No "It's not X — it's Y" structural crutch
- Reflection-to-question ratio 2:1 measured per-session
- No emoji in Cartographer voice
- No bullets/lists in conversation prose
- No clichés ("you're not alone", "many people feel this")

We will write a lint script `pulse/cartographer/anti_slop.py` (deferred to K.7) that scans Cartographer outputs for these patterns and emits warnings during dev.

## Open questions (for K.1+ implementation)

1. **Where does the onboarding session run** — in pulse-chat web (special `?onboarding=1` mode that hides Pulse retrieval indicators and uses Cartographer prompt only)? Or as a separate Pulse-MCP tool exposed through Claude Desktop? **Tentative: web first, MCP follow-up in K.5+.**
2. **Multilingual evidence** — schema fields are language-agnostic but `user_words` are verbatim. Do we tag each user_words entry with language, or trust the user's primary language is fixed? **Tentative: language-tag each entry; allow code-switching mid-evidence.**
3. **Profile versioning** — when schema changes (we add a new field), do we re-run extractor on existing chat history? **Tentative: yes, batch script `migrate_profile.py` per schema version bump.**
4. **Companion ↔ Cartographer separation** — should the companion EVER see the curiosity manifest? Or only the system prompt that biases toward gaps? **Tentative: companion sees the manifest as system context; user never sees it directly until they hover a dashed chip on the map.**

## References

- Original Cartographer v2/v3/v4 prompts: `133/AI beings/garden/garden_cartographer_prompt_v3.md`, `_xml_garden_cartographer_prompt_v4.md`, `cartographer_chat_v3.md` (Obsidian vault)
- Reference output JSON: `examples/nik_v3_profile.json` (this directory)
- MI methodology: Magill et al. (continuously updated meta-analyses)
- PACS attachment assessment: Talia, A., Miller-Bottome, M., & Daniel, S. I. F. (2017)
- Dual Control Model: Janssen, E., & Bancroft, J. (2007)
- ASMR audiovisual: Poerio, G. L. et al. (2018)
