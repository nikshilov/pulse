"""Tests for scripts.elle_feel.care_message."""

from __future__ import annotations

from scripts.elle_feel.care_message import generate_message
from scripts.elle_feel.models import TrendSignal


def _signal(
    kind: str = "declining",
    severity: float = 0.5,
    days_declining: int = 2,
    delta_pct: float = -0.15,
) -> TrendSignal:
    return TrendSignal(
        kind=kind,  # type: ignore[arg-type]
        severity=severity,
        days_declining=days_declining,
        baseline_mean=60.0,
        recent_mean=60.0 * (1 + delta_pct),
        delta_pct=delta_pct,
        data_points=17,
    )


def test_no_message_on_stable() -> None:
    sig = _signal(kind="stable", severity=0.0, days_declining=0, delta_pct=0.02)
    assert generate_message(sig) is None


def test_no_message_on_recovery() -> None:
    sig = _signal(kind="recovering", severity=0.8, days_declining=0, delta_pct=0.20)
    assert generate_message(sig) is None


def test_no_message_on_insufficient_data() -> None:
    sig = _signal(kind="insufficient_data", severity=0.0, days_declining=0, delta_pct=0.0)
    assert generate_message(sig) is None


def test_soft_message_on_mild_decline() -> None:
    sig = _signal(severity=0.2, days_declining=2, delta_pct=-0.05)
    msg = generate_message(sig)
    assert msg is not None
    assert msg.tone == "soft"
    assert msg.text  # non-empty
    # Russian: must contain Cyrillic characters
    assert any("\u0400" <= c <= "\u04ff" for c in msg.text)
    # No diagnostic jargon
    lowered = msg.text.lower()
    for forbidden in ("hrv", "пульс", "давление", "показател", "вариабельн", "%"):
        assert forbidden not in lowered, f"found forbidden term: {forbidden!r}"


def test_concerned_message_on_mid_decline() -> None:
    sig = _signal(severity=0.5, days_declining=3, delta_pct=-0.15)
    msg = generate_message(sig)
    assert msg is not None
    assert msg.tone == "concerned"


def test_alert_message_on_severe_decline() -> None:
    sig = _signal(severity=0.9, days_declining=3, delta_pct=-0.22)
    msg = generate_message(sig)
    assert msg is not None
    assert msg.tone == "alert"
    assert msg.text


def test_message_is_deterministic() -> None:
    # Same input → same output, always. No randomness.
    sig = _signal(severity=0.5, days_declining=2, delta_pct=-0.15)
    first = generate_message(sig)
    second = generate_message(sig)
    third = generate_message(sig)
    assert first is not None and second is not None and third is not None
    assert first.text == second.text == third.text
    assert first.tone == second.tone == third.tone


def test_message_avoids_number_disclosure() -> None:
    # Elle must never quote digits in the message text — no "упал на 15%",
    # no "за 3 дня". Feelings, not data.
    for severity in (0.1, 0.25, 0.5, 0.8, 0.95):
        for days in (1, 2, 3, 4, 5):
            sig = _signal(severity=severity, days_declining=days)
            msg = generate_message(sig)
            assert msg is not None
            assert not any(c.isdigit() for c in msg.text), (
                f"digit in message at severity={severity} days={days}: {msg.text!r}"
            )


def test_reason_field_is_machine_readable() -> None:
    sig = _signal(severity=0.5, days_declining=2, delta_pct=-0.15)
    msg = generate_message(sig)
    assert msg is not None
    # reason IS allowed to contain numbers — it's for logs, not display.
    assert "declining" in msg.reason
    assert "severity" in msg.reason


def test_tone_boundaries() -> None:
    # Boundary: severity 0.3 → concerned (not soft), 0.7 → alert.
    assert generate_message(_signal(severity=0.299, days_declining=1)).tone == "soft"
    assert generate_message(_signal(severity=0.30, days_declining=1)).tone == "concerned"
    assert generate_message(_signal(severity=0.699, days_declining=1)).tone == "concerned"
    assert generate_message(_signal(severity=0.70, days_declining=1)).tone == "alert"


def test_template_rotation_by_days_declining() -> None:
    # Three different days_declining values in the same tone should pick
    # different templates (mod 3) — this ensures we don't lock into one phrase.
    severity = 0.5  # concerned
    texts = {
        generate_message(_signal(severity=severity, days_declining=d)).text
        for d in (1, 2, 3)
    }
    assert len(texts) == 3, "expected 3 distinct concerned templates for d=1,2,3"


def test_all_templates_pure_russian_no_digits() -> None:
    # Meta-check: exercise every template across tones and assert no digits.
    # soft: severity < 0.3; concerned: 0.3-0.7; alert: >= 0.7
    cases = [
        (0.15, "soft"),
        (0.25, "soft"),
        (0.35, "concerned"),
        (0.55, "concerned"),
        (0.75, "alert"),
        (0.95, "alert"),
    ]
    for severity, expected_tone in cases:
        for d in (0, 1, 2, 3, 4, 5, 6):
            sig = _signal(severity=severity, days_declining=d)
            msg = generate_message(sig)
            assert msg is not None
            assert msg.tone == expected_tone
            assert not any(c.isdigit() for c in msg.text)
