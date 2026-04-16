"""Fixture corpus for Pulse retrieval benchmark.

Anchored around the "Elle + Nik" personal-companion domain (the real production
use-case for Pulse). Dates are relative to BENCH_NOW so the recency-decay term
in retrieval._rank is exercised: some entities are fresh (0-3 days), some
medium (20-40 days), some stale (80-120 days).

Entity kinds are diverse — person/project/place/concept/org/thing — and
emotional_weight spans the full [0, 1] range so we can tell whether ranking
actually leverages it.

Data shape mirrors 005_graph.sql exactly (canonical_name, kind, aliases,
first_seen, last_seen, salience_score, emotional_weight, description_md).
Relations/facts are authored by hand to reflect real relationship shapes
(Anna-spouse-Nik, Kristina-ex-Nik, Sonya-stag-partner, Pulse-built_by-Nik,
Garden-related_to-Pulse, etc.).
"""

from datetime import datetime, timedelta, timezone

# Anchor "now" for the bench corpus. All last_seen offsets are computed from
# this so retrieval's recency term is deterministic.
BENCH_NOW = datetime(2026, 4, 16, 12, 0, 0, tzinfo=timezone.utc)


def _iso(days_ago: int) -> str:
    return (BENCH_NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- ENTITIES --------------------------------------------------------------
# Each row: id, canonical_name, kind, aliases, first_seen_days, last_seen_days,
# salience_score, emotional_weight, description
ENTITIES = [
    # ---- Close relationships (high emotional_weight, high salience) ----
    (1, "Anna", "person", ["Аня", "Анна", "жена"], 1200, 1, 0.95, 0.92,
     "Wife. Center of the domestic graph."),
    (2, "Sonya", "person", ["Соня", "Сонечка"], 400, 2, 0.85, 0.88,
     "Stag/hotwife partner. Repair-shaped intimacy."),
    (3, "Nik", "person", ["Никита", "Ник", "Никит"], 2000, 0, 1.0, 0.90,
     "User (self-entity). High salience because he is the graph's origin."),

    # ---- Trauma-adjacent (high emotional_weight, moderate salience) ----
    (4, "Kristina", "person", ["Кристина"], 3500, 95, 0.55, 0.85,
     "Ex from 15-21. Six-year betrayal. Core wound source."),
    (5, "Anna-receiver-wound", "concept", ["receiver damage", "рана-приёмник"],
     600, 45, 0.50, 0.78,
     "4-year oral boycott pattern; receiver broken over years, not innate."),

    # ---- Projects (low-moderate emotional_weight) ----
    (6, "Pulse", "project", ["pulse-engine", "pulse"], 180, 0, 0.90, 0.25,
     "Custom memory engine replacing OpenClaw. Go + Python."),
    (7, "Garden", "project", ["garden-app", "сад"], 365, 5, 0.80, 0.22,
     "iOS companion app + narrative engine. Backend shares Pulse."),

    # ---- Mid-tier people (moderate emotional_weight) ----
    (8, "Fedya", "person", ["Федя", "Фёдор"], 4000, 30, 0.45, 0.55,
     "Anna's son, 12 years old. Shared parenting context."),
    (9, "Eva", "person", ["Ева", "Евочка"], 1000, 7, 0.50, 0.60,
     "Sonya's daughter, 3 years old."),
    (10, "Grace", "person", ["Грейс"], 200, 10, 0.40, 0.35,
     "Opus escalation agent. Colleague-shaped."),

    # ---- Places ----
    (11, "Novosibirsk", "place", ["Новосибирск", "Новосиб"], 5000, 60, 0.30, 0.15,
     "Hometown. Island on Ob river context."),
    (12, "VDS-152", "place", ["152.42.186.145", "vds"], 300, 1, 0.40, 0.05,
     "Digital Ocean VDS hosting Elle."),

    # ---- Concepts (emotional, abstract) ----
    (13, "anxiety", "concept", ["тревога", "тревожно", "беспокойство"],
     1500, 20, 0.35, 0.70,
     "Recurrent anxiety state. Usually surfaces late evenings."),
    (14, "loneliness", "concept", ["одиночество", "мне плохо", "тоска"],
     2000, 14, 0.30, 0.82,
     "Core affective state; surfaces when nobody is confirming existence."),

    # ---- Org / thing (trivial, low emotion) ----
    (15, "Anthropic", "org", ["Anthropic", "антропик"], 500, 40, 0.25, 0.05,
     "Claude provider."),
    (16, "motorcycle", "thing", ["мотоцикл", "байк"], 800, 1, 0.30, 0.40,
     "Nik's motorcycle. Fell 2026-04-15, scraped elbow."),

    # ---- Stale / low-salience fillers ----
    (17, "Krisp", "project", ["krisp"], 400, 110, 0.20, 0.05,
     "Call transcription. Mostly idle."),
    (18, "Tanqueray", "thing", ["Tanqueray", "джин"], 700, 3, 0.20, 0.20,
     "Preferred gin. Fresh because Nik drinks it regularly."),
]


# --- RELATIONS -------------------------------------------------------------
# (from_id, to_id, kind, strength, last_seen_days, context)
RELATIONS = [
    (1, 3, "spouse", 1.0, 1, "Married, daily contact"),
    (3, 1, "spouse", 1.0, 1, "Daily contact"),
    (2, 3, "stag_partner", 0.9, 2, "Repair-shaped intimacy"),
    (3, 2, "stag_partner", 0.9, 2, "Repair-shaped intimacy"),
    (4, 3, "ex", 0.8, 95, "Relationship 15-21"),
    (1, 8, "mother_of", 1.0, 30, "Fedya is Anna's son"),
    (2, 9, "mother_of", 1.0, 7, "Eva is Sonya's daughter"),
    (3, 6, "creator", 0.95, 0, "Builds Pulse"),
    (3, 7, "creator", 0.9, 5, "Builds Garden"),
    (6, 7, "related_to", 0.7, 5, "Shared backend"),
    (3, 16, "owns", 0.8, 1, "Rides daily"),
    (3, 13, "experiences", 0.6, 20, "Anxiety in evenings"),
    (3, 14, "experiences", 0.7, 14, "Core affect"),
    (1, 5, "embodies", 0.75, 45, "Receiver-wound pattern"),
    (6, 12, "runs_on", 0.5, 1, "Pulse runs on VDS"),
    (3, 18, "drinks", 0.5, 3, "Gin preference"),
    (3, 11, "born_in", 0.4, 365, "Hometown"),
    # weak link (below 0.3 threshold in BFS) — should NOT be traversed
    (15, 6, "provides_api_to", 0.2, 40, "Anthropic → Pulse"),
]


# --- FACTS -----------------------------------------------------------------
# (entity_id, text, confidence)
FACTS = [
    (1, "Anna has been silent for 5 years on 'я тебя люблю'", 0.9),
    (1, "Anna does practical body-care but not sensual touch on couch", 0.85),
    (2, "Sonya works at pulse 110 — receiver-side functional", 0.95),
    (4, "Kristina at 12: father's porn echo moment", 0.9),
    (6, "Pulse is written in Go and Python", 0.98),
    (6, "Phase 2e: BFS + salience decay shipped April 2026", 0.95),
    (7, "Garden won 9-way empathic bench with 26.71", 0.95),
    (13, "Anxiety usually peaks after 22:00", 0.8),
    (14, "Loneliness message 'мне плохо' is the canonical no-proper-noun trigger", 0.9),
    (16, "Fell on a turn 2026-04-15, scraped elbow", 0.95),
    (8, "Fedya is 12 years old in 2033 timeline", 0.9),
]


def seed(con):
    """Insert the corpus into a fresh Pulse DB (already migrated).

    Caller is responsible for running migrations before calling seed().
    """
    import json

    for (eid, name, kind, aliases, fs, ls, sal, emo, desc) in ENTITIES:
        con.execute(
            "INSERT INTO entities (id, canonical_name, kind, aliases, first_seen, last_seen, "
            "salience_score, emotional_weight, description_md) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (eid, name, kind, json.dumps(aliases, ensure_ascii=False),
             _iso(fs), _iso(ls), sal, emo, desc),
        )

    for (f, t, kind, strength, ls, ctx) in RELATIONS:
        # first_seen = last_seen for simplicity; context column added in 007_phase_2.sql
        con.execute(
            "INSERT INTO relations (from_entity_id, to_entity_id, kind, strength, "
            "first_seen, last_seen, context) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f, t, kind, strength, _iso(ls), _iso(ls), ctx),
        )

    for (eid, text, conf) in FACTS:
        con.execute(
            "INSERT INTO facts (entity_id, text, confidence, created_at) VALUES (?, ?, ?, ?)",
            (eid, text, conf, _iso(0)),
        )

    con.commit()
