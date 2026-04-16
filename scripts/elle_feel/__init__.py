"""Elle Feel — health-to-emotion signal module for Pulse.

Detects physiological trends in Apple Health data (HRV, etc.) and produces
warm, proactive care messages Elle can send to Nik. She notices without
surveilling. She's a companion, not a nurse.
"""

from scripts.elle_feel.models import HrvPoint, TrendSignal, CareMessage
from scripts.elle_feel.hrv_trend import detect_trend
from scripts.elle_feel.care_message import generate_message
from scripts.elle_feel.integration import check_and_enqueue
from scripts.elle_feel.valence_message import generate_valence_message

__all__ = [
    "HrvPoint",
    "TrendSignal",
    "CareMessage",
    "detect_trend",
    "generate_message",
    "check_and_enqueue",
    "generate_valence_message",
]
