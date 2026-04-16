"""Tests for extraction prompt builder."""

from extract.prompts import build_extract_prompt, EXTRACT_INSTRUCTIONS


def test_build_extract_prompt_includes_graph_context():
    obs = {"source_kind": "telegram", "actors": [{"kind": "user", "id": "123"}], "content_text": "Hello world"}
    ctx = {"existing_entities": [{"id": 1, "canonical_name": "Anna", "kind": "person", "aliases": ["Аня"]}]}
    prompt = build_extract_prompt(obs, ctx)
    assert "id=1 name=Anna kind=person aliases=[Аня]" in prompt
    assert "Hello world" in prompt


def test_build_extract_prompt_handles_empty_graph():
    obs = {"source_kind": "voice", "actors": [], "content_text": "Test content"}
    ctx = {"existing_entities": []}
    prompt = build_extract_prompt(obs, ctx)
    assert "(none)" in prompt
    assert "Test content" in prompt


def test_extract_instructions_no_json_formatting_directives():
    assert "JSON" not in EXTRACT_INSTRUCTIONS
    assert "```" not in EXTRACT_INSTRUCTIONS
    assert "save_extraction" in EXTRACT_INSTRUCTIONS
