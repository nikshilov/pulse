"""Tests for triage prompt builder."""

from extract.prompts import build_triage_prompt, TRIAGE_INSTRUCTIONS


def test_build_triage_prompt_includes_all_observations():
    obs = [
        {"source_kind": "telegram", "actors": [{"kind": "user", "id": "1"}], "content_text": "First msg"},
        {"source_kind": "telegram", "actors": [{"kind": "user", "id": "2"}], "content_text": "Second msg"},
    ]
    prompt = build_triage_prompt(obs)
    assert "1. [telegram" in prompt
    assert "2. [telegram" in prompt
    assert "First msg" in prompt
    assert "Second msg" in prompt


def test_triage_instructions_mention_tool():
    assert "triage_observations" in TRIAGE_INSTRUCTIONS


def test_triage_prompt_truncates_long_content():
    obs = [{"source_kind": "telegram", "actors": [], "content_text": "x" * 1000}]
    prompt = build_triage_prompt(obs)
    assert len(prompt) < 1500
