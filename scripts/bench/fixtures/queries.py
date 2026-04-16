"""Held-out queries for the Pulse retrieval benchmark.

Each query has a `message` (what Nik might type / say), a `ground_truth` set
of entity ids (memories that SHOULD be surfaced for Elle to respond well),
and a short `category` label so the breakdown table can show where retrieval
shines and where it falls over.

Ground-truth design philosophy:
    - For a named-person query, the person entity itself is GT.
    - For a 2-hop query ("Anna's son"), the 2-hop target is GT. The 1-hop
      anchor is NOT ground truth; we want to know if BFS reaches the target.
    - For an emotional no-proper-noun query ("мне плохо"), the GT is the
      CONCEPT entity whose aliases include that phrase — so keyword retrieval
      CAN hit it if alias-matching is working. The harder flavor ("пусто
      сегодня", no token in any alias list) is where keyword genuinely fails.
"""

QUERIES = [
    # ---- Direct canonical name ----
    {
        "id": "q01_direct_name",
        "message": "Что там с Anna?",
        "ground_truth": [1],  # Anna
        "category": "direct-name",
    },
    # ---- Alias match (Cyrillic) ----
    {
        "id": "q02_alias_cyrillic",
        "message": "Аня сегодня устала после работы",
        "ground_truth": [1],
        "category": "alias",
    },
    # ---- Alias for trauma-adjacent person ----
    {
        "id": "q03_alias_trauma",
        "message": "Кристина снова приснилась",
        "ground_truth": [4],  # Kristina
        "category": "alias-trauma",
    },
    # ---- Multi-entity direct ----
    {
        "id": "q04_multi_direct",
        "message": "Надо обсудить Pulse и Garden — как BFS работает",
        "ground_truth": [6, 7],  # Pulse + Garden
        "category": "multi-direct",
    },
    # ---- 2-hop: anchor person, target is relative reachable via relation ----
    # "сын Ани" — Anna is the anchor (found by token 'Ани'), Fedya is 1-hop neighbor.
    {
        "id": "q05_2hop_relative",
        "message": "Как там сын Ани?",
        "ground_truth": [8],  # Fedya — reachable only via Anna→Fedya
        "category": "2-hop-relative",
    },
    # ---- 2-hop: Pulse via anchor 'VDS' (Pulse runs_on VDS) ----
    {
        "id": "q06_2hop_via_place",
        "message": "Что крутится на VDS?",
        "ground_truth": [6],  # Pulse — reachable via VDS-152 → Pulse
        "category": "2-hop-via-place",
    },
    # ---- Emotional / no-proper-noun — testable via concept alias ----
    # 'мне плохо' IS in aliases of entity 14 (loneliness), so keyword SHOULD hit.
    # If it does not, that is the real bug (token n-gram miss).
    {
        "id": "q07_emo_alias_present",
        "message": "мне плохо",
        "ground_truth": [14],  # loneliness
        "category": "emotional-alias",
    },
    # ---- Emotional / NO matching alias (known weakness) ----
    # No entity has 'пусто' or 'сегодня' as an alias. Keyword retrieval MUST
    # fail this — it is the Phase-3-embeddings target. Ground truth is still
    # the best-fit concept (loneliness) so the metric reflects the miss.
    {
        "id": "q08_emo_no_token_weakness",
        "message": "пусто сегодня, ничего не хочется",
        "ground_truth": [14],
        "category": "emotional-gap",
    },
    # ---- Anxiety via Cyrillic alias ----
    {
        "id": "q09_anxiety_alias",
        "message": "опять тревога вечером",
        "ground_truth": [13],  # anxiety
        "category": "emotional-alias",
    },
    # ---- Freshness: fresh entity should outrank stale same-kind peer ----
    # Both Pulse (fresh, 0d) and Krisp (stale, 110d) are projects with the
    # word 'проект' — but we query by their own tokens to test that Pulse
    # (fresh + high emo) ranks above Krisp when both appear in top-10.
    {
        "id": "q10_freshness_fresh",
        "message": "Pulse и Krisp — статус",
        "ground_truth": [6],  # Pulse is the desired top-1 (fresh)
        "category": "freshness-fresh",
    },
    # ---- Freshness: query a stale entity explicitly — low rank expected ----
    {
        "id": "q11_freshness_stale",
        "message": "Krisp что-то слышно?",
        "ground_truth": [17],  # Krisp — stale; GT to confirm it CAN be recalled
        "category": "freshness-stale",
    },
    # ---- Trivia / trivial thing with recent activity ----
    {
        "id": "q12_thing_recent",
        "message": "налей Tanqueray",
        "ground_truth": [18],
        "category": "thing",
    },
    # ---- Self-reference / high-salience person ----
    {
        "id": "q13_self",
        "message": "Никита хочет отдохнуть",
        "ground_truth": [3],  # Nik
        "category": "direct-alias-self",
    },
    # ---- 2-hop via co-project: Garden → Pulse ----
    {
        "id": "q14_2hop_project",
        "message": "Как продвигается Garden?",
        "ground_truth": [7, 6],  # Garden direct, Pulse via related_to
        "category": "2-hop-project",
    },
    # ---- Place query ----
    {
        "id": "q15_place",
        "message": "Новосибирск вспомнился",
        "ground_truth": [11],
        "category": "place",
    },
]
