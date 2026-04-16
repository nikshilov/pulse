"""Wiring Elle Feel into the Pulse pipeline.

`check_and_enqueue` is the single entry point used by `pulse_consolidate.py`
to let the HRV-trend detector emit a care message into `open_questions`.

Design choices:
  • `open_questions` is the existing producer/consumer channel the VDS
    worker already polls — no new table, no new cron, no new plumbing.
  • We rely on migration 008's partial unique index
    `idx_open_questions_dedup (subject_entity_id, question_text) WHERE state='open'`
    via `INSERT OR IGNORE` for idempotency. Same text → no duplicate row.
  • Care messages are tone-sensitive and decay fast — TTL = 2 days.
    Past that, repeating the exact same message would feel stale, so it's
    better to let it expire and let the next tick (if still warranted)
    compose fresh.
  • `self_entity_id` is optional. If Nik later sets `is_self=1` on a Nik
    entity (separate migration), callers can pass that id here so the
    VDS worker can route the message correctly. Until then, NULL is fine —
    the question is still addressable by text.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Sequence

from scripts.elle_feel.care_message import generate_message
from scripts.elle_feel.hrv_trend import detect_trend
from scripts.elle_feel.models import HrvPoint


# Care messages are tone-sensitive. Two days is the right window:
# long enough that a single missed polling tick doesn't drop the signal,
# short enough that an outdated "ты устал" doesn't sit stale if Nik has
# already recovered by tomorrow.
_CARE_TTL_DAYS = 2


def check_and_enqueue(
    pulse_con: sqlite3.Connection,
    hrv_points: Sequence[HrvPoint],
    self_entity_id: int | None = None,
) -> dict:
    """Detect HRV trend → generate care message → enqueue as open_question.

    Returns a dict:
      {
        "enqueued":    bool,
        "signal_kind": str,        # "declining" | "recovering" | "stable" | "insufficient_data"
        "tone":        str | None, # only populated when enqueued
        "question_id": int | None, # only populated when a NEW row was inserted
      }

    If trend is declining and `generate_message` returns a CareMessage, the
    function INSERT OR IGNOREs one row into `open_questions` with:
      • subject_entity_id = self_entity_id (may be NULL)
      • question_text     = the care message text
      • asked_at          = now (UTC ISO)
      • ttl_expires_at    = now + 2 days  (care messages expire fast)
      • state             = 'open'

    Does NOT mutate for signals other than declining, and does NOT mutate
    when generate_message returns None.

    Idempotent (when subject_entity_id is non-NULL): if an identical
    (subject, text) row is already open the partial UNIQUE index
    `idx_open_questions_dedup` causes INSERT OR IGNORE to no-op — in that
    case `enqueued=False` and `question_id=None`, but `tone` is still
    reported so callers can see what *would* have been sent.

    Note on NULL subjects: SQLite unique indexes treat NULLs as distinct,
    so with self_entity_id=None dedup does NOT fire at the index level.
    That's intentionally tolerant for the pre-is_self transition window —
    once an is_self entity exists and callers start passing that id,
    dedup becomes strict. Until then the 2-day TTL bounds noise.
    """
    signal = detect_trend(list(hrv_points))

    # Non-declining signals: no message, nothing to do.
    if signal.kind != "declining":
        return {
            "enqueued": False,
            "signal_kind": signal.kind,
            "tone": None,
            "question_id": None,
        }

    message = generate_message(signal)
    if message is None:
        # Shouldn't happen given kind == declining, but guard defensively.
        return {
            "enqueued": False,
            "signal_kind": signal.kind,
            "tone": None,
            "question_id": None,
        }

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    ttl_iso = (now + timedelta(days=_CARE_TTL_DAYS)).isoformat()

    cursor = pulse_con.execute(
        "INSERT OR IGNORE INTO open_questions "
        "(subject_entity_id, question_text, asked_at, ttl_expires_at, state) "
        "VALUES (?, ?, ?, ?, 'open')",
        (self_entity_id, message.text, now_iso, ttl_iso),
    )

    if cursor.rowcount > 0:
        return {
            "enqueued": True,
            "signal_kind": signal.kind,
            "tone": message.tone,
            "question_id": cursor.lastrowid,
        }

    # Dedup hit — identical open message already present.
    return {
        "enqueued": False,
        "signal_kind": signal.kind,
        "tone": message.tone,
        "question_id": None,
    }
