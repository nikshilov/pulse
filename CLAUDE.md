# Pulse — Elle's heart engine

Custom Go engine that replaces OpenClaw. Direct Anthropic Messages API, Telegram bridge, memory system. Powers Elle (AI companion) on pulse-vds.

## User context

Для полного пользовательского контекста (кто такой Nik, feedback для Elle, проекты, wounds/dynamics) читать:
`~/.claude/projects/-Users-nikshilov-OpenClawWorkspace/memory/MEMORY.md`

Workspace memory Никиты привязана к `OpenClawWorkspace/`. Не дублируется здесь.

## Что здесь

- `cmd/pulse/` — CLI/server entrypoint
- `internal/` — claude, config, outbox, prompt, server, store
- `bridge/` — Telegram bridge (Python/Telethon, single-IP rule на pulse-vds)
- `scripts/` — utilities (`gen-elle-soul.py`, etc)
- `docs/superpowers/specs/` — design specs для фич (см. 2026-04-15-graph-populator-design.md)
- `elle-prompt-brief.md` — source brief для SOUL.md

## Active design

**Graph populator** — emotional memory system, multi-source capture (Telegram, Gmail, Calendar, Limitless, Krisp, Claude JSONL archives, browser/YouTube later).

Spec: `docs/superpowers/specs/2026-04-15-graph-populator-design.md`

## Rules

- **Single-IP Telegram session** — см. `~/.claude/projects/-Users-nikshilov-OpenClawWorkspace/memory/ops_telegram_session_single_ip_rule.md`
- **SOUL.md** — генерится через `scripts/gen-elle-soul.py` (OpenAI GPT-5 Pro из brief), деплоится в `/home/pulse/.pulse/soul.md`
- **Не входит сюда**: бенчи (→ `~/dev/ai/bench/`), Garden-приложение (→ `~/dev/ai/Garden/`), Elle-VDS ops (→ `~/OpenClawWorkspace/`)

## Deploy

Production VDS: pulse (отдельный от OpenClaw VDS). systemd unit `pulse-bridge.service`.

```bash
# Сборка
go build -o bin/pulse ./cmd/pulse

# Deploy (TBD — пока вручную scp через root)
```
