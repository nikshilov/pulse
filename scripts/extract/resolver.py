"""Entity resolution with confidence gates.

Gates (per design v3):
- exact alias / canonical match → auto bind (confidence 1.0)
- 0.7..0.98 → entity_merge_proposals pending
- < 0.7 → new entity + open_question
"""

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Literal, Optional


@dataclass
class ResolutionDecision:
    action: Literal["bind_identity", "proposal", "new_entity_with_question"]
    entity_id: Optional[int]
    confidence: float
    reason: str


def _similarity(a: str, b: str) -> float:
    seq_score = SequenceMatcher(None, a.lower(), b.lower()).ratio()
    # Token-prefix boost: if candidate starts with an existing alias token (or vice versa),
    # treat as a soft first-name match. Handles "Анна Петровна" vs alias "Анна".
    token_score = _token_first_match(a.lower(), b.lower())
    return max(seq_score, token_score)


def _token_first_match(a: str, b: str) -> float:
    """Return 0.85 if the first token of the shorter string equals the first token of the longer."""
    a_tokens = a.split()
    b_tokens = b.split()
    if not a_tokens or not b_tokens:
        return 0.0
    # Exact first-token match (first name match)
    if a_tokens[0] == b_tokens[0]:
        return 0.85
    # Any shared token longer than 2 chars (cross-word alias overlap)
    for at in a_tokens:
        if len(at) > 2 and at in b_tokens:
            return 0.80
    return 0.0


def _best_match(candidate: dict, existing: list[dict]) -> tuple[Optional[dict], float]:
    name = candidate.get("canonical_name", "")
    aliases = candidate.get("aliases") or []
    kind = candidate.get("kind")
    best = None
    best_score = 0.0

    for ent in existing:
        if kind and ent.get("kind") != kind:
            continue
        ent_all = [ent["canonical_name"]] + list(ent.get("aliases") or [])
        cand_all = [name] + list(aliases)
        pair_best = 0.0
        for c in cand_all:
            if not c:
                continue
            for e in ent_all:
                score = _similarity(c, e)
                if score > pair_best:
                    pair_best = score
        if pair_best > best_score:
            best_score = pair_best
            best = ent

    return best, best_score


def resolve_entity(candidate: dict, existing: list[dict]) -> ResolutionDecision:
    best, score = _best_match(candidate, existing)
    if best is None:
        return ResolutionDecision("new_entity_with_question", None, 0.0, "no existing entities of this kind")
    if score >= 0.98:
        return ResolutionDecision("bind_identity", best["id"], score, f"exact match to {best['canonical_name']}")
    if score >= 0.7:
        return ResolutionDecision("proposal", best["id"], score, f"soft match to {best['canonical_name']} at {score:.2f}")
    return ResolutionDecision("new_entity_with_question", None, score, f"best candidate {best['canonical_name']} below 0.7")
