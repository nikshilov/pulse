#!/usr/bin/env python3
"""Telethon bridge for Pulse M1.

Listens to Telegram messages from a specific user, POSTs them to Pulse,
and polls the outbox to deliver replies.

Shows "typing…" action to the user while Pulse is composing a reply.

Required env:
  PULSE_URL         e.g. http://127.0.0.1:18789
  PULSE_SECRET      contents of ~/.pulse/secret.key
  TG_API_ID         Telegram API id
  TG_API_HASH       Telegram API hash
  TG_ALLOWED_USER   Telegram user_id that Pulse responds to
  TG_SESSION        path to telethon session file
"""
import asyncio
import os
import sys

import aiohttp
from telethon import TelegramClient, events

PULSE_URL = os.environ["PULSE_URL"].rstrip("/")
PULSE_SECRET = os.environ["PULSE_SECRET"].strip()
TG_API_ID = int(os.environ["TG_API_ID"])
TG_API_HASH = os.environ["TG_API_HASH"]
TG_ALLOWED_USER = int(os.environ["TG_ALLOWED_USER"])
TG_SESSION = os.environ["TG_SESSION"]

HEADERS = {"X-Pulse-Key": PULSE_SECRET, "Content-Type": "application/json"}

# Safety cap: how long typing stays alive if Pulse never replies.
# Telegram drops SetTyping after ~5s on its own; Telethon's context manager
# re-emits internally so we just need a wall-clock upper bound.
TYPING_MAX_SECONDS = 90

typing_tasks: dict[int, asyncio.Task] = {}


async def keep_typing(tg: TelegramClient, chat_id: int) -> None:
    try:
        async with tg.action(chat_id, "typing"):
            await asyncio.sleep(TYPING_MAX_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"typing loop error chat={chat_id}: {e}", file=sys.stderr)


def stop_typing(chat_id: int) -> None:
    task = typing_tasks.pop(chat_id, None)
    if task and not task.done():
        task.cancel()


def start_typing(tg: TelegramClient, chat_id: int) -> None:
    stop_typing(chat_id)
    typing_tasks[chat_id] = asyncio.create_task(keep_typing(tg, chat_id))


async def post_msg(session: aiohttp.ClientSession, chat_id: int, message_id: int, text: str):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "timestamp": "",
    }
    async with session.post(f"{PULSE_URL}/msg", json=payload, headers=HEADERS) as resp:
        if resp.status not in (200, 202):
            body = await resp.text()
            print(f"POST /msg failed {resp.status}: {body}", file=sys.stderr)


async def outbox_loop(session: aiohttp.ClientSession, tg: TelegramClient):
    while True:
        try:
            async with session.get(f"{PULSE_URL}/outbox?limit=5", headers=HEADERS) as resp:
                if resp.status != 200:
                    await asyncio.sleep(2)
                    continue
                rows = await resp.json()
            for row in rows:
                chat_id = row["chat_id"]
                ok, err = True, ""
                try:
                    await tg.send_message(
                        chat_id,
                        row["text"],
                        reply_to=row.get("reply_to"),
                    )
                except Exception as e:
                    ok, err = False, str(e)
                stop_typing(chat_id)
                ack = {"id": row["id"], "success": ok, "error": err}
                async with session.post(f"{PULSE_URL}/outbox/ack", json=ack, headers=HEADERS) as r:
                    if r.status != 204:
                        print(f"ack failed {r.status}", file=sys.stderr)
        except Exception as e:
            print(f"outbox loop error: {e}", file=sys.stderr)
        await asyncio.sleep(2)


async def main():
    tg = TelegramClient(TG_SESSION, TG_API_ID, TG_API_HASH)
    await tg.start()
    me = await tg.get_me()
    print(f"Logged in as {me.username or me.id}", file=sys.stderr)

    session = aiohttp.ClientSession()

    @tg.on(events.NewMessage(from_users=TG_ALLOWED_USER))
    async def on_msg(event):
        start_typing(tg, event.chat_id)
        await post_msg(session, event.chat_id, event.id, event.raw_text or "")

    asyncio.create_task(outbox_loop(session, tg))
    print("bridge ready", file=sys.stderr)
    await tg.run_until_disconnected()
    await session.close()


if __name__ == "__main__":
    asyncio.run(main())
