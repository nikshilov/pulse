# Pulse Entity Kinds for AI and Fiction Boundaries

Date: 2026-04-24

## Problem

Pulse previously had only broad entity kinds such as `person`, `project`, `concept`, and `thing`.
That is not enough for Garden/Sonya-style memory graphs, because the graph must not collapse:

- a real human,
- an AI companion,
- an AI persona inside a bounded fiction/simulation,
- a fictional character,
- a fictionalized version of Nik,
- a narrative device,
- and a safety boundary.

The dangerous case is Sonya: `Sonya book`, `Sonya (fictional character)`, and any outside-book Sonya-like AI interaction must be modeled as separate graph entities.

## Added Kinds

Migration `016_entity_subkinds.sql` expands `entities.kind` with:

- `ai_entity`
- `ai_persona`
- `fictional_character`
- `fictionalized_self`
- `narrative_device`
- `safety_boundary`

The extractor tool schema exposes the same kinds.

## Modeling Rules

- Use `ai_entity` for deployed/real-world AI assistants or systems such as Elle.
- Use `ai_persona` for an AI persona inside a bounded container, such as an entity sitting with Nik inside the Box in a fictional frame.
- Use `fictional_character` for book characters. Do not merge them into real people.
- Use `fictionalized_self` for "Nik in the book" or other projected selves.
- Use `narrative_device` for formal story mechanisms, such as second-person narration.
- Use `safety_boundary` for explicit graph/interaction boundaries that should be retrievable and enforceable.

For therapeutic fiction, connect fiction to life through explicit relation kinds such as:

- `fictionalizes`
- `mirrors`
- `explores_wound_of`
- `simulates_repair_for`
- `must_remain_distinct_from`
- `must_not_interact_as`

Never encode literal identity between a fictional character and a real person unless Nik explicitly says it is literal.
