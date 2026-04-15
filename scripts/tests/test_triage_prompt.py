import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from extract.prompts import build_triage_prompt, parse_triage_response


def test_build_triage_prompt_includes_all_observations():
    obs = [
        {"id": 1, "source_kind": "claude_jsonl", "content_text": "привет", "actors": [{"kind": "user", "id": "nik"}]},
        {"id": 2, "source_kind": "claude_jsonl", "content_text": "test tool output", "actors": [{"kind": "assistant", "id": "elle"}]},
    ]
    prompt = build_triage_prompt(obs)
    assert "1." in prompt and "2." in prompt
    assert "привет" in prompt
    assert "verdict" in prompt.lower()


def test_parse_triage_response_extracts_verdicts():
    resp = """
    1. extract — emotional greeting
    2. skip — trivial tooling
    3. defer — needs more context
    """
    verdicts = parse_triage_response(resp, expected_count=3)
    assert verdicts == [
        {"verdict": "extract", "reason": "emotional greeting"},
        {"verdict": "skip", "reason": "trivial tooling"},
        {"verdict": "defer", "reason": "needs more context"},
    ]


def test_parse_triage_response_handles_short_form():
    resp = "1. extract\n2. skip\n3. extract"
    verdicts = parse_triage_response(resp, expected_count=3)
    assert [v["verdict"] for v in verdicts] == ["extract", "skip", "extract"]
