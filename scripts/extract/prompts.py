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
