# Graph Populator — Design (multi-source)

**Date:** 2026-04-15
**Author:** Elle (with Nik)
**Status:** draft v2 — multi-source rewrite

## Goal

Elle видит всё, что видит Nik — не только в Telegram, а вообще всё: почту, встречи, записи разговоров (Krisp), записи с пендента (Limitless), историю браузера, YouTube, лайки/просмотры в Instagram, и архив всех разговоров Nik с Claude Code. Слышит, собирает emotional memory в Pulse graph, вечером синкает в Obsidian vault, обсуждает диффы с Nik. Через неделю — градиент автономии: Elle сама решает отвечать/не отвечать на простое в @hey_elle, сложное/близкое — по-прежнему к Nik.

**Критично:** граф — эмоциональная память Nik, a не просто datalake. Каждое наблюдение извлекается в entities / relations / events / sentiment / salience. Источник (Telegram, Gmail, Krisp, Limitless, …) — это измерение наблюдения, не граница графа.

## Scope

**Источники Phase-1 (в этом спеке):**

| Source | Kind | Auth | Priority | Notes |
|--------|------|------|----------|-------|
| Claude Code chats (local JSONL) | batch-import | filesystem | **first** | Огромный архив, уже есть, zero risk. Начинаем отсюда. |
| Telegram — @hey_elle account | realtime-push | existing Telethon session | high | Elle writer+reader. Существующий мост. |
| Telegram — Nik's personal account | realtime-push | new Telethon session | high | Observer only. One-time SMS+2FA, session живёт на pulse-vds. |
| Limitless pendant (lifelogs) | periodic-pull | MCP (уже подключён) | high | Встречи, разговоры оффлайн. |
| Gmail | periodic-pull | MCP (уже подключён) | medium | Деловая переписка, контекст людей. |
| Google Calendar | periodic-pull | MCP (уже подключён) | medium | События, встречи, ритмы. |
| Krisp recordings | realtime-push (webhook есть) | existing webhook | medium | Транскрипты звонков. Сигнал уже приходит в `queue/signals/`. |
| Apple Health | periodic-pull (уже есть) | existing DB | low-context | Не как entity, a как sentiment-модификатор (усталость → вес события). |

**Источники Phase-2 (после Phase-1 стабилен):**

| Source | Kind | How |
|--------|------|-----|
| Browser history | batch-import → periodic-pull | Локальный экспорт Chrome/Safari SQLite → ingest CLI. Потом automator на Mac раз в N часов. |
| YouTube watch history | batch-import | Google Takeout JSON → ingest CLI. Далее — Google Data API если нужен realtime. |
| Instagram (likes, saves, viewed) | batch-import | Instagram data download (ZIP с JSON) → ingest CLI. Meta не даёт realtime — только periodic dumps. |
| Apple Notes / Obsidian (Nik's own) | read-only index | Читаем, не мутируем. `garden/` — его, мы туда только пишем новое. |
| Slack / Discord | realtime-push | Когда понадобится — отдельный bridge по той же схеме. |
| iMessage / SMS | batch-import | Локальный chat.db. Приватно, только с пользовательским запуском. |

**Не включено в Phase-1:**
- Secret chats в Telegram — недоступны через Telethon anyway. Фиксируем только факт "был secret chat с X".
- Photos/голосовые без транскрипта — пишем метаданные ("отправил голосовое в X, длительность Y"), не content.
- Raw audio/video storage — нет. Только транскрипты и результаты extraction.

## Architecture

### Четыре слоя

```
┌────────────────────────────────────────────────────┐
│ 1. CAPTURE (provider adapters)                     │
│  TG-elle │ TG-nik │ Gmail │ Calendar │ Limitless  │
│  Krisp   │ Health │ Claude-JSONL │ Browser │ YT   │
│    ↓ (normalized Observation)                      │
├────────────────────────────────────────────────────┤
│ 2. INGEST (Pulse /ingest endpoint + dispatch)      │
│    validate, dedupe, write observations table      │
│    enqueue for extraction per-batch                │
├────────────────────────────────────────────────────┤
│ 3. EXTRACT (Opus 4.6 worker)                       │
│    observations → entities / relations / events    │
│    entity resolution across source_ids             │
│    salience + emotional_weight scoring             │
├────────────────────────────────────────────────────┤
│ 4. SYNC & ACT                                      │
│    evening diff → Nik DM → regen Obsidian → git   │
│    autonomy gradient for @hey_elle scope           │
└────────────────────────────────────────────────────┘
```

### Provider framework

Каждый источник — **Provider** с одним из трёх профилей:

**realtime-push** (telegram, krisp):
- Бежит 24/7 (systemd service или long-lived connection).
- Услышал событие → нормализует в Observation → POST `/ingest`.
- Examples: Telethon bridge, Krisp webhook.

**periodic-pull** (limitless, gmail, calendar, health):
- Cron-триггер (каждые N минут/часов).
- Знает свой last_seen_cursor (в Pulse DB).
- Пуллит новое → POST `/ingest` batch.
- Examples: MCP clients через Pulse worker.

**batch-import** (claude-jsonl, browser dumps, takeout archives):
- On-demand CLI: `pulse ingest --source=claude-jsonl --path=~/.claude/projects/`.
- Читает локальные файлы, нормализует, POST `/ingest` батчами.
- Idempotent: повторный запуск не дублирует (dedupe по source_kind + source_id).

**Общий contract (Provider interface):**

```go
// internal/capture/provider.go
type Provider interface {
    Kind() SourceKind       // "telegram", "gmail", "limitless", "claude_jsonl", ...
    Mode() Mode             // RealtimePush | PeriodicPull | BatchImport
    Normalize(raw any) ([]Observation, error)
}

type Observation struct {
    SourceKind   string          // "telegram" | "gmail" | "limitless" | ...
    SourceID     string          // unique-per-source: tg:chat/msg_id, gmail:message-id, ...
    Scope        string          // "elle" | "nik" | "shared"
    CapturedAt   time.Time       // когда event произошёл
    ObservedAt   time.Time       // когда мы его увидели
    Actors       []ActorRef      // кто участвовал (source-specific ids)
    ContentText  string          // нормализованный текст
    MediaRefs    []MediaRef      // без binary — только метаданные
    Metadata     map[string]any  // source-specific (chat_kind, reply_to, labels, url, …)
    RawJSON      json.RawMessage // для re-extraction без re-ingest
}
```

Providers производят Observations. Everything downstream работает только с Observations — не знает про Telegram/Gmail/etc.

### Data model

**Pulse DB (SQLite на pulse-vds, migrations via embedded sql):**

```sql
-- Raw normalized events
CREATE TABLE observations (
  id            INTEGER PRIMARY KEY,
  source_kind   TEXT NOT NULL,             -- 'telegram'|'gmail'|'limitless'|'krisp'|'claude_jsonl'|...
  source_id     TEXT NOT NULL,             -- unique within source_kind
  scope         TEXT NOT NULL,             -- 'elle'|'nik'|'shared'
  captured_at   DATETIME NOT NULL,
  observed_at   DATETIME NOT NULL,
  actors        JSON NOT NULL,             -- [{kind, id, display}, …]
  content_text  TEXT,
  media_refs    JSON,
  metadata      JSON,
  raw_json      JSON,
  UNIQUE(source_kind, source_id)
);
CREATE INDEX ix_obs_captured ON observations(captured_at);
CREATE INDEX ix_obs_scope    ON observations(scope, captured_at);

-- Canonical entities (people, places, projects, orgs, things)
CREATE TABLE entities (
  id                INTEGER PRIMARY KEY,
  canonical_name    TEXT NOT NULL,
  kind              TEXT NOT NULL,         -- 'person'|'place'|'project'|'org'|'thing'|'event_series'
  aliases           JSON,
  first_seen        DATETIME,
  last_seen         DATETIME,
  salience_score    REAL DEFAULT 0,
  emotional_weight  REAL DEFAULT 0,
  description_md    TEXT
);

-- One entity → many source identifiers
CREATE TABLE entity_identities (
  id           INTEGER PRIMARY KEY,
  entity_id    INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  source_kind  TEXT NOT NULL,              -- 'telegram'|'gmail'|'phone'|'limitless_speaker'|...
  identifier   TEXT NOT NULL,              -- tg_user_id|email|phone|speaker_fingerprint
  confidence   REAL DEFAULT 1.0,
  first_seen   DATETIME,
  UNIQUE(source_kind, identifier)
);
CREATE INDEX ix_identities_entity ON entity_identities(entity_id);

-- Relations between entities
CREATE TABLE relations (
  id                 INTEGER PRIMARY KEY,
  from_entity_id     INTEGER NOT NULL REFERENCES entities(id),
  to_entity_id       INTEGER NOT NULL REFERENCES entities(id),
  kind               TEXT NOT NULL,        -- 'spouse'|'colleague'|'therapist'|'friend'|'parent'|...
  strength           REAL DEFAULT 0,
  first_seen         DATETIME,
  last_seen          DATETIME,
  evidence_obs_ids   JSON
);

-- Facts (atomic claims about entities)
CREATE TABLE facts (
  id                 INTEGER PRIMARY KEY,
  entity_id          INTEGER NOT NULL REFERENCES entities(id),
  text               TEXT NOT NULL,
  confidence         REAL DEFAULT 1.0,
  evidence_obs_ids   JSON,
  created_at         DATETIME NOT NULL
);

-- Events (happenings with emotional weight)
CREATE TABLE events (
  id                 INTEGER PRIMARY KEY,
  title              TEXT NOT NULL,
  description        TEXT,
  sentiment          REAL,                 -- [-1, 1]
  emotional_weight   REAL DEFAULT 0,
  entity_ids         JSON,                 -- participants
  ts                 DATETIME NOT NULL,
  evidence_obs_ids   JSON
);

-- Open questions Elle holds (for evening sync)
CREATE TABLE open_questions (
  id                INTEGER PRIMARY KEY,
  subject_entity_id INTEGER REFERENCES entities(id),
  question_text     TEXT NOT NULL,
  asked_at          DATETIME NOT NULL,
  answered_at       DATETIME,
  answer_text       TEXT
);

-- Autonomy log: every Elle reply in @hey_elle scope
CREATE TABLE elle_draft_decisions (
  id               INTEGER PRIMARY KEY,
  scope            TEXT NOT NULL,          -- 'elle_dm'|'elle_group'|'nik_dm_ghost'|...
  context_obs_ids  JSON,
  elle_proposed    TEXT,
  nik_edited_to    TEXT,
  kept_unchanged   BOOLEAN,
  sent_at          DATETIME
);

-- Per-provider cursor for periodic-pull
CREATE TABLE provider_cursors (
  source_kind  TEXT PRIMARY KEY,
  cursor       TEXT NOT NULL,              -- e.g. gmail history_id, limitless ts, ...
  updated_at   DATETIME NOT NULL
);
```

### Entity resolution

Когда extractor видит актора с identifier `{kind: telegram, id: 12345}` или `{kind: email, id: kira@...}`:

1. Lookup `entity_identities` по (source_kind, identifier). Если match → это существующий entity.
2. Если нет — попытаться soft-match:
   - По имени/email local part / phone (если видны).
   - По контексту (тот же thread, те же другие участники).
3. Если soft-match confidence ≥ 0.7 → привязать как дополнительный identity к существующему entity.
4. Иначе — создать новый entity + первый identity. Добавить в open_questions: "Кто такой X? Новый контакт / alias существующего / случайный?".

Обе LLM-пассы идут с полным контекстом и доступом к существующему графу, так что resolution — часть extraction промпта, не отдельный микросервис.

### Obsidian vault layout (`garden/graph/`)

```
people/<CanonicalName>.md          — dossier per-entity
events/<YYYY-MM-DD>-<slug>.md      — events с высоким emotional_weight
projects/<Name>.md
places/<Name>.md
sources/<ChatName or Meeting>.md   — per-conversation summary
threads/<Topic>.md                 — cross-source тема (напр. "Пхукет" — из Telegram + Krisp + Calendar)
_index.md                          — regen каждый sync
```

Всё с `[[wikilinks]]`. Obsidian строит граф сам. Визуализатор не делаем.

`threads/` — важно: это кросс-источниковое связывание. Один topic ("Пхукет"), discussions в Telegram-чате с Аней + Krisp-звонок с агентом + Calendar-событие "виза" — все три линкуются в thread.

## Scopes & behavior matrix (Telegram)

| Scope | Listen | Extract | Respond |
|-------|--------|---------|---------|
| @hey_elle DM с Nik | yes | yes | yes (existing) |
| @hey_elle DM со strangers | yes | yes (+dossier) | draft → Nik → send |
| @hey_elle groups — тегают её | yes | yes | yes (live) |
| @hey_elle groups — не тегают | yes | yes | no |
| Nik DM с Аней | yes | **priority** | NEVER send |
| Nik DM с терапевтом | yes | **priority** | NEVER send |
| Nik DM со strangers | yes | yes (+dossier) | draft → Nik DM via @hey_elle → Nik sends |
| Nik groups | yes | yes | NEVER send |

**Strict rule:** никогда не отправка от имени Nik без per-message explicit OK. Даже в week-2+ autonomy — автономия только на @hey_elle.

Для других источников "respond" не применимо (только listen+extract). Gmail drafts в будущем (Phase-2+).

## Evening sync protocol

**Trigger:** cron 23:00 GMT+7 ИЛИ Nik пишет "синк сейчас" / "что за день".

**Elle to Nik** (пример):
```
Вечер. Собрала за сегодня:
— 3 новые entities: [[Марк К.]] (tg ЛС, работа),
  [[Lada's kindergarten #3]] (Аня упомянула), [[Garden M2]] (чат разработки)
— 4 update в existing: [[Anna]] (2 события, emotional_weight высокий —
  посмотри events/anna-morning.md), [[Fedya]] (школа, новое), 
  [[терапевт]] (инсайт), [[Kira]] (gmail — confirmed meetup)
— 2 открытых вопроса:
  1. Марк К. — друг, работа, случайный?
  2. Ты в Krisp-звонке упомянул "поездка в мае" — куда?

Регенерить `garden/graph/`?
```

**Flow:**
1. Nik отвечает (правит entities, закрывает open_questions).
2. Elle применяет изменения в Pulse DB.
3. Регенерит `garden/graph/` (Obsidian подхватывает iCloud sync).
4. Git commit + push в Garden repo.

## Autonomy gradient (week-1+)

**Log:** `elle_draft_decisions` пишет ВСЕ решения Elle в @hey_elle scope: что предложила, что Nik изменил/одобрил/отверг, в каком контексте (references obs_ids).

**Weekly pattern review** (Sonnet job, воскресенье):
- "Твои правки за неделю: 80% ты оставляешь мой draft без изменений для X-class. 60% отвергаешь для Y-class. Готов передать X-class мне?"

**Per-class handoff:** Nik явно OK → Elle auto-sends для конкретного паттерна. Не blanket. Каждый класс отдельно.

**Классы на старте:**
- `elle_dm_smalltalk` — быстрая реплика.
- `elle_dm_scam_spam` — очевидное — игнор/блок.
- `elle_dm_business_first_contact` — холодный контакт → draft to Nik.
- `elle_group_mention_tech` — тех-вопрос с тегом.

Сложные близкие отношения, эмоции, работа, семья — всегда через Nik.

## Technical stack

**Pulse side (Go):**
- `internal/capture/` — Provider interface + adapters (telegram, claude_jsonl, limitless, gmail, ...).
- `internal/ingest/` — `/ingest` endpoint, validate, dedupe, write observations.
- `internal/graph/` — extractor (Opus call), entity resolution, upsert entities/relations/events.
- `internal/sync/` — diff gen, Obsidian markdown writer, git integration.
- `internal/autonomy/` — draft decision log, weekly pattern job.
- `cmd/pulse-ingest/` — CLI для batch-import (`pulse ingest --source=claude-jsonl --path=…`).

**Bridges / workers:**
- `scripts/telethon-bridge-elle.py` (existing) — @hey_elle realtime.
- `scripts/telethon-bridge-nik.py` (new) — Nik's account observer.
- `scripts/limitless-poller.py` — MCP → `/ingest` loop (cron).
- `scripts/gmail-poller.py` — MCP → `/ingest` loop (cron).
- `scripts/calendar-poller.py` — same.
- `scripts/krisp-webhook-to-ingest.sh` — мост существующего webhook к `/ingest`.

**Models:**
- Extraction: **Opus 4.6** (high) — эмоциональная нюансировка, salience. Core.
- Entity resolution: встроено в extraction prompt (тот же call).
- Diff summary / weekly review: **Sonnet 4.6** (medium) — ежедневная дешёвая работа.
- Autonomy decisions: **Sonnet 4.6** с full graph context.

## Claude JSONL batch import — specifics

**Why first:** огромный объём локально, zero internet dependency, zero privacy risk, все данные уже у нас. Идеальный первый источник чтобы обкатать extractor.

**Source:** `~/.claude/projects/*/*.jsonl` на Mac Nik. Каждая jsonl-сессия — диалог Nik ↔ Claude (ENI / Elle / Grace / иной агент).

**Нормализация:**
- `source_kind = "claude_jsonl"`, `scope = "shared"` (разговор Nik с AI-собой).
- `source_id = "{session_file}:{line_index}"`.
- `actors`: `[{kind: "user", id: "nik"}, {kind: "assistant", id: "<agent-name-from-cwd>"}]`.
- `content_text`: user message OR assistant textual content (не tool_use, не thinking).
- `metadata`: `{session_id, cwd, git_branch, model}` — полезно для scope-фильтра.
- Skip: `isMeta: true`, tool_result, системные XML.

**CLI:**
```
pulse ingest --source=claude-jsonl --path=~/.claude/projects/ [--since=2025-01-01]
```

Идемпотент: по UNIQUE(source_kind, source_id) — повторный запуск не дублирует.

**Что извлекаем:** entities (люди/места/проекты которые Nik обсуждал с ИИ), events (моменты инсайтов, решений, эмоциональных всплесков), facts (что Nik хотел от AI, о чём переживал). **Особая ценность:** архив показывает эволюцию Nik-AI отношений — контекст для Elle почему она такая.

## 2FA / Telegram session setup (nik-bridge)

**Новая сессия `nik-tg-session.session`:**
1. Nik даёт API_ID + API_HASH (user-level, не @hey_elle).
2. Nik даёт номер телефона → SMS/Telegram-код.
3. Script `scripts/bootstrap-nik-session.py` на pulse-vds: sign-in flow, сохраняет сессию в `/home/pulse/.pulse/secrets/nik-session.session`.
4. 2FA пароль — Nik говорит один раз, в скрипт.
5. Systemd: `nik-bridge.service` (user=pulse), same pattern как elle-bridge.

**Single-IP rule:** файл сессии никогда не копируется. Живёт только на pulse-vds. Nik's phone/mac — свои auth_keys, не трогаем. (См. `ops_telegram_session_single_ip_rule.md`.)

## Milestones

**M1. Capture framework + Observations**
- `internal/capture/provider.go` interface.
- `observations`, `entity_identities`, `provider_cursors` migrations.
- `/ingest` endpoint: validate, dedupe по UNIQUE(source_kind, source_id), write.
- CLI skeleton `cmd/pulse-ingest/`.
- No extraction yet. Pure capture.

**M2. Claude JSONL batch import** ⬅ первый живой источник
- `internal/capture/providers/claude_jsonl.go` — Normalize.
- `pulse ingest --source=claude-jsonl` работает на полном архиве.
- Проверить: counts, spot-check нормализации.

**M3. Extractor + graph core**
- `internal/graph/` extractor: Opus call, parse to entities/relations/events/facts.
- Entity resolution via entity_identities (part of extraction prompt).
- Batch trigger: по 200 observations ИЛИ 1 час inactivity.
- Schema migrations: entities, relations, facts, events, open_questions.
- Tests on fixture transcripts.
- Прогнать extractor на claude_jsonl batch → проверить graph naglami.

**M4. Telegram live (оба scope)**
- `scripts/telethon-bridge-nik.py` (new) — observer на Nik's account.
- Расширить `telethon-bridge-elle.py`: слушать все её группы + strangers DM.
- Normalize → POST `/ingest`. Extract идёт через общую пайплайн из M3.
- Deploy + verify.

**M5. MCP pullers (Limitless + Gmail + Calendar)**
- `scripts/{limitless,gmail,calendar}-poller.py` — cron loops.
- `provider_cursors` update после каждого батча.
- Нормализация meetings/emails/events в Observation.
- Tests на sample data.

**M6. Evening sync ritual**
- `internal/sync/`: diff generator (new/updated/unresolved), Obsidian markdown writer.
- Cron 23:00 GMT+7.
- Elle DM with diff via existing outbox.
- Nik replies → Pulse processes → regen Obsidian → git commit + push (в отдельный Garden repo).

**M7. Autonomy gradient**
- `elle_draft_decisions` table (уже в schema) — capture every Elle reply.
- Weekly Sunday pattern job.
- Per-class handoff mechanism.

**M8 (Phase-2). Krisp, Health-as-modifier, browser, YouTube, Instagram**
- Krisp webhook → `/ingest` adapter (shortest hop — сигнал уже приходит).
- Health: extractor учитывает emotional_weight модификатор при low HRV / bad sleep.
- Browser/YouTube/Instagram — batch-import via `pulse ingest`.
- Новых схем не нужно — обычные providers.

**M1→M3 = core.** M4–M5 = breadth. M6 = интерфейс. M7 = доверие. M8 = полнота.

## What this is NOT

- Не CRM. Никаких "due for follow-up". Relationships, не leads.
- Не автоответчик. Autonomy = Elle как коллега, не как робот.
- Не surveillance. Граф — эмоциональная память Nik, owned by Nik, git-reproducible.
- Не замена Obsidian notes Nik. Nik пишет руками в vault — не трогаем его файлы. Пишем только в `garden/graph/`.
- Не datalake. Мы **не храним** raw audio / video / images — только транскрипты, метаданные, ссылки.
- Не единственный источник правды. Если противоречие в графе — Nik прав, Elle правит память по его слову.

## Open questions (to resolve before plan)

1. **Retention raw observations:** хранить raw_json вечно или архивировать > 90 дней? (Предложение: вечно пока диск позволяет. Графа без raw не проверишь.)
2. **Scope колонки:** `elle|nik|shared` достаточно, или нужен multi-scope per-obs? (Предложение: одиночный scope, достаточно.)
3. **Health integration:** modifier при extraction (emotional_weight+) или отдельная табличка `context_snapshots`? (Предложение: modifier на Phase-1, таблица в Phase-2 если захотим трендов.)
4. **Git push target:** `nikshilov/garden.git` (отдельный репо от Pulse) ✓ — уже решили.
5. **Секрет-chats / encrypted:** фиксируем только факт ("был secret chat с X, длительность Y"). OK?
6. **Instagram/YouTube**: насколько глубоко парсим? Только likes+saves, или также comments+DMs? (Предложение: Phase-2, likes+saves+watched-video-titles; DMs — позже.)

## Why this architecture fits

Центральный вопрос Nik был: "ЛОЖИТСЯ ЛИ ЭТО НА АРХИТЕКТУРУ СЕЙЧАС?"

**Ответ: да, ложится. Один рефакторинг — замена `observed_messages` на `observations` + добавление `entity_identities`.** Всё downstream (extractor, sync, autonomy) работает с абстрактной Observation, не знает про Telegram.

- **Capture слой** пл-пл. Один interface, адаптер на источник. Добавить Slack/iMessage — новый файл на 200 строк, графа не трогать.
- **Extractor** source-agnostic: читает Observations, пишет entities/relations/events.
- **Entity resolution** кросс-источниковое by design (entity_identities).
- **Obsidian sync** агрегирует по entity, не по источнику — dossier [[Anna]] тянет evidence и из Telegram, и из Gmail, и из Krisp.
- **Salience/emotional_weight** работает одинаково для любого источника. Bench memory-ретривала не зависит от capture слоя.

**Bench rerun?** Нет. Bench измеряет retrieval/salience memory системы, a не capture. Схема retrieval не меняется.

---

**Approval gate:** Nik читает → да/правки → save final → writing-plans skill → bite-sized tasks → код по SDD.
