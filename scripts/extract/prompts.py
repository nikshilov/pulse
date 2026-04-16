"""Prompts for two-pass extractor: Sonnet triage + Opus extract.

With Phase 2 tool-use, the model output structure is enforced by tool schemas.
These prompts provide semantic guidance — WHAT to extract, not HOW to format it.

Prompt caching:
- `build_triage_prompt_parts` returns (static_prefix, dynamic_suffix). The static
  prefix contains TRIAGE_INSTRUCTIONS and is cacheable across calls via
  `cache_control: {type: "ephemeral"}`. The dynamic suffix holds the specific
  observation lines.
- `build_extract_prompt_parts` returns (static_prefix, dynamic_suffix). Static =
  EXTRACT_INSTRUCTIONS + tool-use framing (always the same). Dynamic =
  existing_entities block + the specific observation body (these change per call
  so are kept in the uncached suffix).
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


# Static framing that is identical across every triage call. Separate from
# TRIAGE_INSTRUCTIONS so the static-prefix cache tier can grow without changing
# the documented instruction text.
TRIAGE_STATIC_PREFIX = TRIAGE_INSTRUCTIONS + "\nObservations:\n"


def build_triage_prompt(observations) -> str:
    """Legacy single-string builder (kept for backward compatibility)."""
    static, dynamic = build_triage_prompt_parts(observations)
    return static + dynamic


def build_triage_prompt_parts(observations) -> tuple[str, str]:
    """Return (static_prefix, dynamic_suffix) for prompt-cache use.

    static_prefix — invariant instructions + framing; marked ephemeral in the
    Anthropic call so repeated invocations share a cache entry.
    dynamic_suffix — the observation lines (change every call).
    """
    lines: list[str] = []
    for i, obs in enumerate(observations, 1):
        actors = ", ".join(
            f"{a.get('kind', '?')}:{a.get('id', '?')}"
            for a in obs.get("actors", [])
        )
        text = (obs.get("content_text") or "").replace("\n", " ")[:500]
        lines.append(f"{i}. [{obs.get('source_kind')} | {actors}] {text}")
    lines.append("")
    lines.append("Classify each observation now.")
    return TRIAGE_STATIC_PREFIX, "\n".join(lines)


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


# Static framing for the Opus extract call. existing_entities is NOT here — it
# varies per call (top-K by observation relevance) so it lives in the dynamic
# suffix. Caching existing_entities would require a second cache tier and is
# premature optimization at current scale (see task spec, option (a)).
EXTRACT_STATIC_PREFIX = (
    EXTRACT_INSTRUCTIONS
    + "\nCall the save_extraction tool to record entities/relations/events/facts/merge_candidates.\n"
)


def build_extract_prompt(observation: dict, graph_context: dict) -> str:
    """Legacy single-string builder (kept for backward compatibility)."""
    static, dynamic = build_extract_prompt_parts(observation, graph_context)
    return static + dynamic


def build_extract_prompt_parts(
    observation: dict, graph_context: dict
) -> tuple[str, str]:
    """Return (static_prefix, dynamic_suffix) for the Opus extract prompt.

    static_prefix — EXTRACT_INSTRUCTIONS + tool-use framing (invariant, cacheable).
    dynamic_suffix — existing_entities block + the observation body.
    """
    existing = graph_context.get("existing_entities", [])
    existing_lines: list[str] = []
    for e in existing:
        aliases = ", ".join(e.get("aliases") or [])
        existing_lines.append(
            f"  - id={e['id']} name={e['canonical_name']} kind={e['kind']} aliases=[{aliases}]"
        )

    actors = ", ".join(
        f"{a.get('kind')}:{a.get('id')}" for a in observation.get("actors", [])
    )
    dynamic = "\n".join([
        "",
        "Existing entities in the graph:",
        "\n".join(existing_lines) if existing_lines else "  (none)",
        "",
        f"Observation (source={observation.get('source_kind')}, actors={actors}):",
        observation.get("content_text", ""),
    ])
    return EXTRACT_STATIC_PREFIX, dynamic
