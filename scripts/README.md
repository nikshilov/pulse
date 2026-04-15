# Pulse M1 Bridge

Run alongside a local `pulse` binary to ferry Telegram messages.

## Install deps
    pip install telethon aiohttp

## Run
    export PULSE_URL=http://127.0.0.1:3800
    export PULSE_SECRET=$(cat ~/.pulse/secret.key)
    export TG_API_ID=...              # from https://my.telegram.org/apps
    export TG_API_HASH=...             # from https://my.telegram.org/apps
    export TG_ALLOWED_USER=73937064   # your own Telegram numeric user_id
    export TG_SESSION=~/.pulse/telethon.session
    python3 telethon-bridge-m1.py

## M1 caveats

- **Pulse must be up when messages arrive.** If pulse is down when Telegram delivers a message, the bridge logs the failed POST and drops the message — there's no inbox buffer. Restart pulse and resend.
- **Double-send race.** If the bridge successfully sends a reply to Telegram but fails to POST /outbox/ack (network blip, pulse restart between send and ack), the message is re-claimed next cycle and sent a second time. Single-user low-probability; tracked for M2.
