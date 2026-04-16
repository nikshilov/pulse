"""Prompts for two-pass extractor: Sonnet triage + Opus extract.

With Phase 2 tool-use, the model output structure is enforced by tool schemas.
These prompts provide semantic guidance — WHAT to extract, not HOW to format it.
"""


TRIAGE_INSTRUCTIONS = """You are the triage filter for a personal knowledge-graph extraction pipeline.

For each numbered observation, classify it using the triage_observations tool.

Verdicts:
- extract: contains people, places, projects, emotions, decisions, or meaningful events
- skip: trivial content (greetings, emoji-only, tool output, mechanical noise)
- defer: ambiguous, needs more context — will be retried later

Be aggressive about extracting — when in doubt, choose extract over skip.
Only skip truly empty observations.
"""


def build_triage_prompt(observations) -> str:
    lines = [TRIAGE_INSTRUCTIONS, "", "Observations:"]
    for i, obs in enumerate(observations, 1):
        actors = ", ".join(
            f"{a.get('kind', '?')}:{a.get('id', '?')}"
            for a in obs.get("actors", [])
        )
        text = (obs.get("content_text") or "").replace("\n", " ")[:500]
        lines.append(f"{i}. [{obs.get('source_kind')} | {actors}] {text}")
    lines.append("")
    lines.append("Classify each observation now.")
    return "\n".join(lines)


EXTRACT_INSTRUCTIONS = """You are the knowledge-graph extractor for a personal AI assistant.

Given an observation from someone's life (chat message, voice memo, meeting note),
extract structured knowledge using the save_extraction tool.

Extract:
- entities: people, places, projects, organizations, products, communities, skills, concepts
- relations: connections between entities, with qualifying context
- events: notable happenings with timestamps when available
- facts: atomic claims about entities with confidence scores
- merge_candidates: if an extracted entity might match an existing one

Scoring guidance:
- salience (0-1): how important is this entity to the person's life
- emotional_weight (0-1): how emotionally charged (0=neutral, 1=therapist-level)
- sentiment (-1..1): positive/negative valence of events

Ground every extraction in the observation's content. Don't hallucinate.
If a name matches an existing entity alias, prefer the existing entity.
"""


def build_extract_prompt(observation: dict, graph_context: dict) -> str:
    existing = graph_context.get("existing_entities", [])
    existing_lines = []
    for e in existing:
        aliases = ", ".join(e.get("aliases") or [])
        existing_lines.append(
            f"  - id={e['id']} name={e['canonical_name']} kind={e['kind']} aliases=[{aliases}]"
        )

    actors = ", ".join(
        f"{a.get('kind')}:{a.get('id')}" for a in observation.get("actors", [])
    )
    return "\n".join([
        EXTRACT_INSTRUCTIONS,
        "",
        "Existing entities in the graph:",
        "\n".join(existing_lines) if existing_lines else "  (none)",
        "",
        f"Observation (source={observation.get('source_kind')}, actors={actors}):",
        observation.get("content_text", ""),
    ])
