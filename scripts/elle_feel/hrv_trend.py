"""HRV trend detection.

Pure-stdlib implementation. Takes a list of daily HRV readings and returns
a TrendSignal describing whether the recent window has declined, recovered,
or stayed stable compared to a longer baseline.
"""

from __future__ import annotations

from statistics import mean
from typing import Sequence

from scripts.elle_feel.models import HrvPoint, TrendSignal

# Thresholds — tuned to be conservative. A 10% shift is meaningful in HRV
# terms for a multi-day rolling window (daily HRV is noisy; trends aren't).
_SIGNIFICANT_DELTA = 0.10
# Severity normalizes at 25% decline/recovery — that's "clearly something
# going on" territory, so severity caps at 1.0 there.
_SEVERITY_FULL_SCALE = 0.25
# If more than 30% of days in the expected window are missing, we can't
# trust the signal — return insufficient_data rather than a false positive.
_MAX_MISSING_FRACTION = 0.30


def detect_trend(
    points: Sequence[HrvPoint],
    baseline_days: int = 14,
    recent_days: int = 3,
) -> TrendSignal:
    """Detect the recent HRV trend vs baseline.

    Algorithm:
      1. Need >= baseline_days + recent_days observations; else insufficient_data.
      2. Sort by day. Check expected span (span of days from earliest to latest
         relevant point) vs observations — if >30% gap, insufficient_data.
      3. recent = most recent `recent_days` observations.
         baseline = preceding `baseline_days` observations (before recent).
      4. delta_pct = (recent_mean - baseline_mean) / baseline_mean.
      5. delta_pct <= -0.10  → declining (severity = |delta|/0.25, capped at 1.0)
         delta_pct >= +0.10  → recovering (same severity scale)
         else                → stable (severity 0)
      6. days_declining: number of consecutive recent days (counting from
         the most recent backwards) where hrv < baseline_mean. Only nonzero
         when kind == declining.
    """
    if baseline_days <= 0 or recent_days <= 0:
        raise ValueError("baseline_days and recent_days must be positive")

    needed = baseline_days + recent_days

    if len(points) < needed:
        return _insufficient(points)

    ordered = sorted(points, key=lambda p: p.day)

    # Take only the last `needed` observations — if caller passed more history,
    # we still reason about the most recent window.
    window = ordered[-needed:]

    # Gap check: span in calendar days from first to last point of the window.
    expected_span_days = (window[-1].day - window[0].day).days + 1
    if expected_span_days <= 0:
        return _insufficient(window)

    observed = len(window)
    missing_fraction = 1.0 - (observed / expected_span_days)
    # If the calendar span is much larger than the number of observations,
    # the data is too sparse to trust.
    if missing_fraction > _MAX_MISSING_FRACTION:
        return TrendSignal(
            kind="insufficient_data",
            severity=0.0,
            days_declining=0,
            baseline_mean=0.0,
            recent_mean=0.0,
            delta_pct=0.0,
            data_points=observed,
        )

    recent = window[-recent_days:]
    baseline = window[:-recent_days]

    # Shouldn't happen given the length check, but guard anyway.
    if not baseline or not recent:
        return _insufficient(window)

    baseline_mean = mean(p.hrv_ms for p in baseline)
    recent_mean = mean(p.hrv_ms for p in recent)

    if baseline_mean <= 0:
        return _insufficient(window)

    delta_pct = (recent_mean - baseline_mean) / baseline_mean

    if delta_pct <= -_SIGNIFICANT_DELTA:
        severity = min(1.0, abs(delta_pct) / _SEVERITY_FULL_SCALE)
        days_declining = _count_consecutive_below(recent, baseline_mean)
        return TrendSignal(
            kind="declining",
            severity=severity,
            days_declining=days_declining,
            baseline_mean=baseline_mean,
            recent_mean=recent_mean,
            delta_pct=delta_pct,
            data_points=observed,
        )

    if delta_pct >= _SIGNIFICANT_DELTA:
        severity = min(1.0, abs(delta_pct) / _SEVERITY_FULL_SCALE)
        return TrendSignal(
            kind="recovering",
            severity=severity,
            days_declining=0,
            baseline_mean=baseline_mean,
            recent_mean=recent_mean,
            delta_pct=delta_pct,
            data_points=observed,
        )

    return TrendSignal(
        kind="stable",
        severity=0.0,
        days_declining=0,
        baseline_mean=baseline_mean,
        recent_mean=recent_mean,
        delta_pct=delta_pct,
        data_points=observed,
    )


def _count_consecutive_below(recent: Sequence[HrvPoint], baseline_mean: float) -> int:
    """Count consecutive days from the most recent backwards where hrv < baseline."""
    count = 0
    for point in reversed(recent):
        if point.hrv_ms < baseline_mean:
            count += 1
        else:
            break
    return count


def _insufficient(points: Sequence[HrvPoint]) -> TrendSignal:
    return TrendSignal(
        kind="insufficient_data",
        severity=0.0,
        days_declining=0,
        baseline_mean=0.0,
        recent_mean=0.0,
        delta_pct=0.0,
        data_points=len(points),
    )
