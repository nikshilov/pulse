"""Prompts for two-pass extractor: Sonnet triage + Opus extract."""

import re
from typing import Iterable

TRIAGE_INSTRUCTIONS = """You are the triage filter for an emotional memory graph.
For each numbered observation below, decide:
- `extract` — has people/places/projects, emotions, decisions, or meaningful events
- `skip` — trivial (tool output, mechanical, noise)
- `defer` — ambiguous, needs more context later

Respond with ONE line per observation, format:
<number>. <verdict> — <one-line reason>

Be strict: default to skip if no humans or emotional weight.
"""


def build_triage_prompt(observations: Iterable[dict]) -> str:
    lines = [TRIAGE_INSTRUCTIONS, "", "Observations:"]
    for i, obs in enumerate(observations, 1):
        actors = ", ".join(
            f"{a.get('kind', '?')}:{a.get('id', '?')}"
            for a in obs.get("actors", [])
        )
        text = (obs.get("content_text") or "").replace("\n", " ")[:500]
        lines.append(f"{i}. [{obs.get('source_kind')} | {actors}] {text}")
    lines.append("")
    lines.append("Give your verdict lines now.")
    return "\n".join(lines)


_VERDICT_RE = re.compile(
    r"^\s*(\d+)\.\s*(extract|skip|defer)\b(?:\s*[—\-:]\s*(.*))?$",
    re.IGNORECASE,
)


def parse_triage_response(response: str, expected_count: int) -> list[dict]:
    verdicts: dict[int, dict] = {}
    for line in response.splitlines():
        m = _VERDICT_RE.match(line.strip())
        if not m:
            continue
        n = int(m.group(1))
        v = m.group(2).lower()
        reason = (m.group(3) or "").strip()
        verdicts[n] = {"verdict": v, "reason": reason}

    return [
        verdicts.get(i, {"verdict": "defer", "reason": "missing from response"})
        for i in range(1, expected_count + 1)
    ]


import json as _json

EXTRACT_INSTRUCTIONS = """You are the emotional memory extractor.

Given an observation from Nik's life, extract:
- entities (people, places, projects, orgs, things mentioned)
- relations between entities (from, to, kind, strength 0-1)
- events (title, sentiment -1..1, emotional_weight 0-1, ts, entities involved)
- facts (atomic claims about entities: text, confidence 0-1)
- merge_candidates (if you see an entity that might be the same as an existing one, list with confidence 0-1)

For scoring:
- salience: how important is this entity to Nik's life (0-1)
- emotional_weight: how emotionally charged (0-1, 1=Anna/therapist-level, 0=random colleague)
- sentiment: positive/negative valence (-1..1)

Ground every entity/relation/fact/event in the observation's content.
If you see a name that matches an existing entity alias, prefer existing_entities.
Return STRICTLY valid JSON with keys: entities, relations, events, facts, merge_candidates.
"""


def build_extract_prompt(observation: dict, graph_context: dict) -> str:
    existing = graph_context.get("existing_entities", [])
    existing_lines = []
    for e in existing:
        aliases = ", ".join(e.get("aliases") or [])
        existing_lines.append(f"  - id={e['id']} name={e['canonical_name']} kind={e['kind']} aliases=[{aliases}]")

    actors = ", ".join(f"{a.get('kind')}:{a.get('id')}" for a in observation.get("actors", []))
    return "\n".join([
        EXTRACT_INSTRUCTIONS,
        "",
        "Existing entities:",
        "\n".join(existing_lines) if existing_lines else "  (none)",
        "",
        f"Observation (source={observation.get('source_kind')}, actors={actors}):",
        observation.get("content_text", ""),
        "",
        "Respond with JSON only:",
    ])


def parse_extract_response(response: str) -> dict:
    text = response.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)

    data = _json.loads(text)
    for key in ("entities", "relations", "events", "facts", "merge_candidates"):
        data.setdefault(key, [])
    return data
