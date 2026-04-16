"""Care messages for a declining valence (sentiment) trend.

Parallel to care_message.py but consumes the dict that
`pulse_consolidate.valence_trend` returns. Same warm tone — Elle notices
a stretch of low-sentiment days and gently checks in, no diagnostics,
no "your sentiment dropped 12%".

Determinism: the chosen template is a stable function of `data_points`
so repeat calls with the same trend dict pick the same text. That plays
well with the partial UNIQUE dedup index on open_questions.
"""

from __future__ import annotations


# Three templates. Short, warm, no numbers. Valence-trend downward drift
# is a slower/softer signal than HRV collapse — stay gentler than
# care_message's concerned/alert tiers.
_VALENCE_TEMPLATES: tuple[str, ...] = (
    "Никит, последние дни кажется что фон тяжёлый. Не спрашиваю что — просто отмечаю что я здесь.",
    "Слушай, у меня ощущение что неделя вышла плотная не по делам, а по внутреннему весу. Если захочешь выдохнуть вслух — я рядом.",
    "Замечаю тебя. Что-то последние дни с настроением не так — не разговор навязываю, просто чтобы ты знал что я вижу.",
)


def generate_valence_message(trend_dict: dict) -> str | None:
    """Produce a care-message text for a declining valence trend, or None.

    Returns None unless:
      • trend == "declining"
      • data_points >= 7   (enough signal to trust — matches consolidate.py gate)

    The caller (`pulse_consolidate.run_consolidation`) applies the same gates
    before calling — this is belt-and-suspenders so the helper is safe to
    call from anywhere without leaking false positives.
    """
    if trend_dict.get("trend") != "declining":
        return None
    if int(trend_dict.get("data_points", 0)) < 7:
        return None

    # Deterministic template pick keyed on data_points. Quantized modulo 3
    # means the same trend dict always produces the same text → dedup index
    # suppresses duplicate inserts naturally.
    index = int(trend_dict["data_points"]) % len(_VALENCE_TEMPLATES)
    return _VALENCE_TEMPLATES[index]
