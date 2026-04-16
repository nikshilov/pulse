"""Anthropic tool-use schemas for triage and extraction."""

ENTITY_KINDS = [
    "person", "place", "project", "org", "product",
    "community", "skill", "concept", "thing", "event_series",
]

EXTRACT_TOOL = {
    "name": "save_extraction",
    "description": "Save extracted knowledge graph data from the observation",
    "input_schema": {
        "type": "object",
        "required": ["entities", "relations", "events", "facts"],
        "properties": {
            "entities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["canonical_name", "kind"],
                    "properties": {
                        "canonical_name": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Primary name for this entity",
                        },
                        "kind": {
                            "type": "string",
                            "enum": ENTITY_KINDS,
                        },
                        "aliases": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "salience": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                            "description": "How important to Nik's life (0-1)",
                        },
                        "emotional_weight": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                            "description": "Emotional charge (0=neutral, 1=Anna/therapist-level)",
                        },
                    },
                },
            },
            "relations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["from", "to", "kind"],
                    "properties": {
                        "from": {"type": "string", "minLength": 1},
                        "to": {"type": "string", "minLength": 1},
                        "kind": {
                            "type": "string",
                            "description": "Relationship type: colleague, spouse, friend, uses, member_of, etc.",
                        },
                        "context": {
                            "type": "string",
                            "description": "Qualifying context: 'through Cherry Peak', 'from St. Petersburg'",
                        },
                        "strength": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                },
            },
            "events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["title", "entities_involved"],
                    "properties": {
                        "title": {"type": "string", "minLength": 1},
                        "description": {"type": "string"},
                        "sentiment": {"type": "number", "minimum": -1, "maximum": 1},
                        "emotional_weight": {"type": "number", "minimum": 0, "maximum": 1},
                        "ts": {"type": "string", "description": "ISO 8601 timestamp"},
                        "entities_involved": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                        },
                    },
                },
            },
            "facts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["entity", "text"],
                    "properties": {
                        "entity": {"type": "string", "minLength": 1},
                        "text": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Atomic factual claim about this entity",
                        },
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                },
            },
            "merge_candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "new_name": {"type": "string"},
                        "existing_id": {"type": "integer"},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                },
            },
        },
    },
}

TRIAGE_TOOL = {
    "name": "triage_observations",
    "description": "Classify observations for extraction",
    "input_schema": {
        "type": "object",
        "required": ["verdicts"],
        "properties": {
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["index", "verdict", "reason"],
                    "properties": {
                        "index": {"type": "integer", "minimum": 1},
                        "verdict": {
                            "type": "string",
                            "enum": ["extract", "skip", "defer"],
                        },
                        "reason": {"type": "string"},
                    },
                },
            },
        },
    },
}
