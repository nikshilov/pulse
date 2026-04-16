"""Validate tool schema structure for Anthropic API."""

from extract.tool_schemas import EXTRACT_TOOL, TRIAGE_TOOL, ENTITY_KINDS


def test_extract_tool_has_required_top_level_keys():
    assert EXTRACT_TOOL["name"] == "save_extraction"
    assert "input_schema" in EXTRACT_TOOL
    schema = EXTRACT_TOOL["input_schema"]
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"entities", "relations", "events", "facts"}


def test_extract_tool_entity_has_required_fields():
    entity_schema = EXTRACT_TOOL["input_schema"]["properties"]["entities"]["items"]
    assert set(entity_schema["required"]) == {"canonical_name", "kind"}
    assert entity_schema["properties"]["kind"]["enum"] == ENTITY_KINDS


def test_extract_tool_entity_kind_enum_has_10_values():
    assert len(ENTITY_KINDS) == 10
    assert "person" in ENTITY_KINDS
    assert "product" in ENTITY_KINDS
    assert "community" in ENTITY_KINDS
    assert "skill" in ENTITY_KINDS
    assert "concept" in ENTITY_KINDS


def test_triage_tool_has_required_structure():
    assert TRIAGE_TOOL["name"] == "triage_observations"
    verdict_schema = TRIAGE_TOOL["input_schema"]["properties"]["verdicts"]["items"]
    assert set(verdict_schema["required"]) == {"index", "verdict", "reason"}
    assert verdict_schema["properties"]["verdict"]["enum"] == ["extract", "skip", "defer"]


def test_extract_tool_relation_has_context_field():
    rel_schema = EXTRACT_TOOL["input_schema"]["properties"]["relations"]["items"]
    assert "context" in rel_schema["properties"]


def test_extract_tool_fact_has_required_fields():
    fact_schema = EXTRACT_TOOL["input_schema"]["properties"]["facts"]["items"]
    assert set(fact_schema["required"]) == {"entity", "text"}
