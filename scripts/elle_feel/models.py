"""Dataclasses for the Elle Feel module.

These are the small, explicit contracts between the detection layer
(hrv_trend) and the message generation layer (care_message).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

TrendKind = Literal["declining", "recovering", "stable", "insufficient_data"]
MessageTone = Literal["soft", "concerned", "alert"]


@dataclass(frozen=True)
class HrvPoint:
    """A single day of HRV data.

    day: calendar day (not datetime) — HRV is averaged per-day.
    hrv_ms: mean HRV for that day in milliseconds.
    """

    day: date
    hrv_ms: float


@dataclass(frozen=True)
class TrendSignal:
    """Summary of an HRV trend analysis.

    Emitted by detect_trend and consumed by generate_message.
    """

    kind: TrendKind
    severity: float  # 0.0..1.0 — 0 = normal, 1 = emergency
    days_declining: int  # consecutive recent days below baseline mean (0 if not declining)
    baseline_mean: float
    recent_mean: float
    delta_pct: float  # signed: (recent - baseline) / baseline
    data_points: int


@dataclass(frozen=True)
class CareMessage:
    """A message Elle can proactively send to Nik based on a detected signal.

    text: the message, in Russian, first-person from Elle.
    tone: soft | concerned | alert — controls how the caller may want to
          deliver it (e.g. timing, whether to interrupt).
    reason: machine-readable why — for logging, not for display.
    """

    text: str
    tone: MessageTone
    reason: str
