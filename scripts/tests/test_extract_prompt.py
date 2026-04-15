import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import json
from extract.prompts import build_extract_prompt, parse_extract_response

def test_build_extract_prompt_includes_graph_context():
    obs = {"id": 1, "source_kind":"claude_jsonl", "content_text":"Аня сказала что Федя пошёл в школу", "actors":[{"kind":"user","id":"nik"}]}
    graph_context = {
        "existing_entities": [
            {"id": 10, "canonical_name": "Anna", "kind": "person", "aliases": ["Аня"]},
        ],
    }
    prompt = build_extract_prompt(obs, graph_context)
    assert "Anna" in prompt
    assert "Аня" in prompt or "Анна" in prompt
    assert "JSON" in prompt

def test_parse_extract_response_valid_json():
    resp = json.dumps({
        "entities": [
            {"canonical_name": "Anna", "kind": "person", "aliases": ["Аня"], "salience": 0.9, "emotional_weight": 0.8},
            {"canonical_name": "Fedya", "kind": "person", "aliases": ["Федя"], "salience": 0.5, "emotional_weight": 0.3},
        ],
        "relations": [
            {"from": "Anna", "to": "Fedya", "kind": "parent", "strength": 0.9},
        ],
        "events": [
            {"title": "Fedya started school", "sentiment": 0.7, "emotional_weight": 0.4, "ts": "2026-04-15T00:00:00Z", "entities": ["Fedya"]},
        ],
        "facts": [],
        "merge_candidates": [],
    })
    out = parse_extract_response(resp)
    assert len(out["entities"]) == 2
    assert out["relations"][0]["kind"] == "parent"

def test_parse_extract_response_tolerates_code_fence():
    resp = "```json\n{\"entities\":[],\"relations\":[],\"events\":[],\"facts\":[],\"merge_candidates\":[]}\n```"
    out = parse_extract_response(resp)
    assert out["entities"] == []
