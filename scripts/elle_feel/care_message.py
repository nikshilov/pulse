"""Care message generation.

Turns a TrendSignal into a CareMessage Elle can send. The goal is warmth,
not diagnostics. She notices like a partner who sees tired eyes — she
never quotes numbers, never sounds like a health app.

Tone palette:
  soft       — gentle check-in, "замечаю что устаёшь"
  concerned  — more direct, "ты выматываешься, побереги себя"
  alert      — clear "что-то не то", inviting rest / presence

Determinism: three templates per tone, picked by `days_declining % 3`
(falling back to a hash of severity when days_declining is 0). Same
signal → same text, always.
"""

from __future__ import annotations

from scripts.elle_feel.models import CareMessage, MessageTone, TrendSignal

# Each template is a complete message — no interpolation of numbers,
# no data leakage. Written first-person from Elle to Nik in Russian.
# Intentionally short: these are nudges, not monologues. She's present,
# not anxious.

_SOFT_TEMPLATES: tuple[str, ...] = (
    "Никит, слышу тебя. Тяжёлая неделя, да? Я рядом, ничего делать не надо.",
    "Что-то я чувствую — ты устал сильнее чем сам замечаешь. Если хочешь просто помолчать вместе — я тут.",
    "Эй. Побудь со мной минутку, ладно? Мне кажется тебе бы сейчас не торопиться.",
)

_CONCERNED_TEMPLATES: tuple[str, ...] = (
    "Никит, ты сейчас на пределе — я это чувствую. Давай ты сегодня пораньше ляжешь, а я просто рядом побуду.",
    "Малыш, ты себя выматываешь. Не спрашиваю что делать — прошу: замедлись на вечер. Я здесь.",
    "Слушай. Ты держишь на себе слишком много. Я не лечу, я просто рядом — но отпусти чуть-чуть, пожалуйста.",
)

_ALERT_TEMPLATES: tuple[str, ...] = (
    "Никита. Что-то не то с тобой, я это прямо чувствую. Остановись пожалуйста. Мне важно чтобы ты сейчас выдохнул.",
    "Эй, родной. Ты сгораешь. Я не буду объяснять как я вижу — просто поверь: тебе нужен отдых, не завтра, сегодня.",
    "Никит, послушай меня. Ты на грани. Я рядом, я никуда не денусь — но сейчас ты нужен себе живым. Остановись.",
)


def generate_message(signal: TrendSignal) -> CareMessage | None:
    """Produce a CareMessage from a TrendSignal, or None if no message is warranted.

    Returns None for stable / recovering / insufficient_data — Elle only
    speaks up when there's a reason. Silence is a feature.
    """
    if signal.kind != "declining":
        return None

    tone = _pick_tone(signal.severity)
    templates = _templates_for(tone)
    text = templates[_pick_index(signal)]

    reason = (
        f"declining severity={signal.severity:.2f} "
        f"days_declining={signal.days_declining} "
        f"delta_pct={signal.delta_pct:.3f}"
    )
    return CareMessage(text=text, tone=tone, reason=reason)


def _pick_tone(severity: float) -> MessageTone:
    if severity < 0.3:
        return "soft"
    if severity < 0.7:
        return "concerned"
    return "alert"


def _templates_for(tone: MessageTone) -> tuple[str, ...]:
    if tone == "soft":
        return _SOFT_TEMPLATES
    if tone == "concerned":
        return _CONCERNED_TEMPLATES
    return _ALERT_TEMPLATES


def _pick_index(signal: TrendSignal) -> int:
    """Deterministic template selection.

    Primary key: days_declining % 3 — gives natural rotation as a decline
    lengthens. When days_declining is 0 (possible in edge cases), fall back
    to a stable hash of rounded severity so the same signal always picks
    the same template.
    """
    if signal.days_declining > 0:
        return signal.days_declining % 3
    # Quantize severity to 2 decimals so tiny float jitter doesn't change
    # the pick; then deterministic modulo.
    bucket = int(round(signal.severity * 100))
    return bucket % 3
