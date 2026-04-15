# Graph Populator — Design (multi-source, v3)

**Date:** 2026-04-15
**Author:** Elle (with Nik)
**Status:** approved draft v3 — post-review consolidation
**Changes vs v2:** merge proposals w/ confidence gates, sensitive-actor allowlist, content-hash + revisions for edit-idempotency, persistent extraction queue, two-pass extractor, versioned scorer, evening sync one-liner UX + dossier split, erasure protocol, autonomy moved to M8.

## Goal

Elle видит всё, что видит Nik — не только Telegram, a всё: почту, встречи, записи разговоров (Krisp), записи с пендента (Limitless), историю браузера, YouTube, лайки Instagram и архив разговоров Nik с Claude Code. Собирает emotional memory в Pulse graph. Вечером синкает одностраничное сегодняшнее saldo в Obsidian vault и обсуждает диффы с Nik. Через время — градиент автономии: Elle сама отвечает на простое в @hey_elle, сложное/близкое — по-прежнему к Nik.

**Критично:** граф — это эмоциональная память Nik, не datalake. Каждое наблюдение извлекается в entities / relations / events / facts с salience + emotional_weight. Источник (Telegram, Gmail, Krisp, Limitless, …) — измерение наблюдения, не граница графа.

## Scope

**Phase-1 sources (этот спек):**

| Source | Kind | Auth | Priority | Notes |
|--------|------|------|----------|-------|
| Claude Code chats (local JSONL) | batch-import | filesystem | **first** | Огромный архив, zero risk, zero internet dep. Идеально для прогрева extractor. |
| Telegram — @hey_elle account | realtime-push | existing Telethon session | high | Elle writer+reader. Существующий мост. |
| Telegram — Nik's personal account | realtime-push | new Telethon session | high | Observer only. One-time SMS+2FA. |
| Limitless pendant (lifelogs) | periodic-pull | MCP (уже подключён) | high | Встречи, оффлайн-разговоры. |
| Gmail | periodic-pull | MCP (уже подключён) | medium | Деловая переписка, контекст людей. |
| Google Calendar | periodic-pull | MCP (уже подключён) | medium | События, ритмы. |
| Krisp recordings | realtime-push (webhook есть) | existing webhook | medium | Транскрипты звонков. |
| Apple Health | periodic-pull (уже есть) | existing DB | modifier | Не entity — модификатор emotional_weight. |

**Phase-2 sources (после Phase-1 стабилен):**

| Source | Kind | How |
|--------|------|-----|
| Browser history | batch→periodic | Локальный SQLite-экспорт → ingest CLI, потом automator. |
| YouTube watch history | batch | Google Takeout JSON → CLI. |
| Instagram likes/saves/views | batch | Instagram data download (ZIP) → CLI. Meta не даёт realtime. |
| Slack / Discord | realtime-push | Отдельный bridge по той же схеме. |
| iMessage / SMS | batch | Локальный chat.db, только ручной запуск. |
| Apple Notes / Obsidian (Nik's own) | read-only index | Читаем, не мутируем. |

**Не включено в Phase-1:**
- Secret chats Telegram — недоступны через Telethon. Пишем только факт "был secret chat с X".
- Photos/голосовые без транскрипта — метаданные, не content.
- Raw audio/video — не храним. Только транскрипты и результаты extraction.

## Architecture

### Четыре слоя

```
┌────────────────────────────────────────────────────┐
│ 1. CAPTURE (provider adapters)                     │
│    TG-elle │ TG-nik │ Gmail │ Calendar │ Limitless │
│    Krisp   │ Health │ Claude-JSONL │ Browser │ YT  │
│           ↓ (normalized Observation)               │
├────────────────────────────────────────────────────┤
│ 2. INGEST (Pulse /ingest endpoint + dispatch)      │
│    validate → dedupe/revision-detect →              │
│    write observations → enqueue extraction_jobs    │
├────────────────────────────────────────────────────┤
│ 3. EXTRACT (two-pass: Sonnet triage → Opus)        │
│    triage filters noise (~70% skipped) →           │
│    Opus extracts entities/relations/events/facts → │
│    entity resolution → merge proposals → score     │
├────────────────────────────────────────────────────┤
│ 4. SYNC & ACT                                      │
│    one-liner DM + dossier updates → Obsidian →     │
│    git commit. Autonomy gradient for @hey_elle.    │
└────────────────────────────────────────────────────┘
```

### Provider framework

Каждый источник — **Provider** с одним из трёх режимов:

**realtime-push** (telegram, krisp): 24/7 long-lived → Normalize → POST `/ingest`.

**periodic-pull** (limitless, gmail, calendar, health): cron-triggered, знает свой `last_seen_cursor` в `provider_cursors`, пуллит новое → batch POST.

**batch-import** (claude-jsonl, browser dumps, takeout): on-demand CLI, идемпотент по `(source_kind, source_id, content_hash)`.

**Contract:**

```go
// internal/capture/provider.go (финал — Go после M3)
type Provider interface {
    Kind() SourceKind       // "telegram", "gmail", "limitless", "claude_jsonl", ...
    Mode() Mode             // RealtimePush | PeriodicPull | BatchImport
    Normalize(raw any) ([]Observation, error)
}

type Observation struct {
    SourceKind   string          // "telegram" | "gmail" | "limitless" | ...
    SourceID     string          // unique-per-source
    ContentHash  string          // sha256 of normalized content_text — detects edits
    Version      int             // bumped on revision
    Scope        string          // "elle" | "nik" | "shared"
    CapturedAt   time.Time
    ObservedAt   time.Time
    Actors       []ActorRef
    ContentText  string
    MediaRefs    []MediaRef
    Metadata     map[string]any
    RawJSON      json.RawMessage
}
```

M1-M3 — реализация на Python (single-binary скрипты + Pulse Go server для `/ingest`); Go-интерфейс выше — целевая форма с M4. Причина: первые три этапа — исследование/тюнинг extractor и resolution, Python быстрее итерировать; когда форма устаканится, портируем под Go interface.

### Data model (Pulse SQLite)

```sql
-- Raw normalized events (append-only, edits → revisions)
CREATE TABLE observations (
  id            INTEGER PRIMARY KEY,
  source_kind   TEXT NOT NULL,
  source_id     TEXT NOT NULL,
  content_hash  TEXT NOT NULL,             -- sha256(content_text | metadata)
  version       INTEGER NOT NULL DEFAULT 1,
  scope         TEXT NOT NULL,             -- 'elle'|'nik'|'shared'
  captured_at   DATETIME NOT NULL,
  observed_at   DATETIME NOT NULL,
  actors        JSON NOT NULL,
  content_text  TEXT,
  media_refs    JSON,
  metadata      JSON,
  raw_json      JSON,
  redacted      BOOLEAN DEFAULT 0,         -- GDPR erasure flag
  UNIQUE(source_kind, source_id, version)
);
CREATE INDEX ix_obs_captured ON observations(captured_at);
CREATE INDEX ix_obs_scope    ON observations(scope, captured_at);

-- Edit history: когда Telegram message редактируется, или Gmail thread обновляется
CREATE TABLE observation_revisions (
  observation_id INTEGER NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
  version        INTEGER NOT NULL,
  prev_hash      TEXT,
  diff           TEXT,                      -- human-readable diff
  changed_at     DATETIME NOT NULL,
  PRIMARY KEY (observation_id, version)
);

-- Persistent extraction queue (durable, survives crashes)
CREATE TABLE extraction_jobs (
  id              INTEGER PRIMARY KEY,
  observation_ids JSON NOT NULL,             -- batch of obs
  state           TEXT NOT NULL,             -- 'pending'|'running'|'done'|'failed'|'dlq'
  attempts        INTEGER DEFAULT 0,
  last_error      TEXT,
  triage_model    TEXT,                      -- 'sonnet-4.6'
  extract_model   TEXT,                      -- 'opus-4.6'
  triage_verdict  TEXT,                      -- 'extract'|'skip'|'defer'
  created_at      DATETIME NOT NULL,
  updated_at      DATETIME NOT NULL
);
CREATE INDEX ix_extraction_state ON extraction_jobs(state, created_at);

-- Canonical entities
CREATE TABLE entities (
  id                INTEGER PRIMARY KEY,
  canonical_name    TEXT NOT NULL,
  kind              TEXT NOT NULL,         -- 'person'|'place'|'project'|'org'|'thing'|'event_series'
  aliases           JSON,
  first_seen        DATETIME,
  last_seen         DATETIME,
  salience_score    REAL DEFAULT 0,
  emotional_weight  REAL DEFAULT 0,
  scorer_version    TEXT,                  -- 'v1.0' — for drift handling
  description_md    TEXT
);

-- One entity → many source identifiers
CREATE TABLE entity_identities (
  id           INTEGER PRIMARY KEY,
  entity_id    INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  source_kind  TEXT NOT NULL,
  identifier   TEXT NOT NULL,
  confidence   REAL DEFAULT 1.0,
  first_seen   DATETIME,
  UNIQUE(source_kind, identifier)
);
CREATE INDEX ix_identities_entity ON entity_identities(entity_id);

-- Entity merge proposals — confidence-gated human-in-loop
CREATE TABLE entity_merge_proposals (
  id            INTEGER PRIMARY KEY,
  from_entity_id INTEGER NOT NULL REFERENCES entities(id),
  to_entity_id   INTEGER NOT NULL REFERENCES entities(id),
  confidence     REAL NOT NULL,            -- 0.7..0.98 → proposal, ≥0.98 auto, <0.7 open_question
  evidence_md    TEXT NOT NULL,
  state          TEXT NOT NULL,            -- 'pending'|'approved'|'rejected'|'auto_merged'
  proposed_at    DATETIME NOT NULL,
  resolved_at    DATETIME,
  resolved_by    TEXT                      -- 'nik' | 'auto'
);

-- Sensitive actors allowlist
CREATE TABLE sensitive_actors (
  entity_id   INTEGER PRIMARY KEY REFERENCES entities(id),
  policy      TEXT NOT NULL,               -- 'redact_content'|'summary_only'|'no_capture'
  reason      TEXT,
  added_at    DATETIME NOT NULL,
  added_by    TEXT                         -- 'nik'
);

-- Relations between entities
CREATE TABLE relations (
  id                 INTEGER PRIMARY KEY,
  from_entity_id     INTEGER NOT NULL REFERENCES entities(id),
  to_entity_id       INTEGER NOT NULL REFERENCES entities(id),
  kind               TEXT NOT NULL,        -- 'spouse'|'colleague'|'therapist'|'friend'|'parent'|...
  strength           REAL DEFAULT 0,
  first_seen         DATETIME,
  last_seen          DATETIME
);

-- Facts (atomic claims)
CREATE TABLE facts (
  id                 INTEGER PRIMARY KEY,
  entity_id          INTEGER NOT NULL REFERENCES entities(id),
  text               TEXT NOT NULL,
  confidence         REAL DEFAULT 1.0,
  scorer_version     TEXT,
  created_at         DATETIME NOT NULL
);

-- Events (happenings with emotional weight)
CREATE TABLE events (
  id                 INTEGER PRIMARY KEY,
  title              TEXT NOT NULL,
  description        TEXT,
  sentiment          REAL,
  emotional_weight   REAL DEFAULT 0,
  scorer_version     TEXT,
  ts                 DATETIME NOT NULL
);

-- Normalized evidence (replaces JSON arrays in relations/facts/events)
CREATE TABLE evidence (
  id               INTEGER PRIMARY KEY,
  subject_kind     TEXT NOT NULL,           -- 'relation'|'fact'|'event'|'entity'
  subject_id       INTEGER NOT NULL,
  observation_id   INTEGER NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
  weight           REAL DEFAULT 1.0,
  created_at       DATETIME NOT NULL
);
CREATE INDEX ix_evidence_subject ON evidence(subject_kind, subject_id);
CREATE INDEX ix_evidence_obs     ON evidence(observation_id);

-- Score history for drift/version changes
CREATE TABLE score_history (
  id               INTEGER PRIMARY KEY,
  subject_kind     TEXT NOT NULL,           -- 'entity'|'event'|'fact'
  subject_id       INTEGER NOT NULL,
  salience         REAL,
  emotional_weight REAL,
  sentiment        REAL,
  scorer_version   TEXT NOT NULL,
  computed_at      DATETIME NOT NULL
);
CREATE INDEX ix_score_subject ON score_history(subject_kind, subject_id, computed_at);

-- Open questions Elle holds
CREATE TABLE open_questions (
  id                INTEGER PRIMARY KEY,
  subject_entity_id INTEGER REFERENCES entities(id),
  question_text     TEXT NOT NULL,
  asked_at          DATETIME NOT NULL,
  ttl_expires_at    DATETIME NOT NULL,      -- asked_at + 7 days
  answered_at       DATETIME,
  answer_text       TEXT,
  state             TEXT NOT NULL           -- 'open'|'answered'|'expired'|'auto_closed'
);

-- Autonomy log
CREATE TABLE elle_draft_decisions (
  id               INTEGER PRIMARY KEY,
  scope            TEXT NOT NULL,           -- 'elle_dm_smalltalk'|'elle_dm_scam_spam'|...
  context_obs_ids  JSON,
  elle_proposed    TEXT,
  nik_edited_to    TEXT,
  kept_unchanged   BOOLEAN,
  sent_at          DATETIME
);

-- Per-provider cursor
CREATE TABLE provider_cursors (
  source_kind  TEXT PRIMARY KEY,
  cursor       TEXT NOT NULL,
  updated_at   DATETIME NOT NULL
);
```

### Entity resolution — confidence gates

Когда extractor видит актора с `(source_kind, identifier)`:

1. **Exact identifier match** → существующий entity, привязать observation.
2. **Soft-match** (имя, email local part, phone, context-overlap) → вычислить confidence:
   - `≥ 0.98` → **auto_merged**: молча привязать identity, запись в `entity_merge_proposals` как audit trail (state='auto_merged').
   - `0.7..0.98` → **pending proposal**: создать entity_merge_proposals, показать в evening sync, Nik решает approve/reject.
   - `< 0.7` → **new entity + open_question** ("Кто такой Марк из @hey_elle? Новый или alias существующего?").
3. Open_questions имеют TTL 7 дней. Expired → state='auto_closed', Nik их не увидит снова (шум уходит).
4. Merge proposals не auto-expire — они ждут Nik.

Обе LLM-пассы идут с полным контекстом существующего графа, resolution — часть extraction промпта.

### Two-pass extractor

**Проблема:** Opus 4.6 дорогой; большая часть observations — шум (технические сообщения, рутина, неэмоциональные).

**Решение:**

**Pass 1 — Sonnet 4.6 triage** (~$0.003/obs):
- Input: observation (compact).
- Output: verdict `extract` / `skip` / `defer` + one-line reason.
- Batching: до 50 observations в один Sonnet call (cheap bulk).

**Pass 2 — Opus 4.6 extract** (~$0.05/obs) — только для verdict=extract:
- Input: observation + relevant graph context (existing entities, recent relations).
- Output: entities/relations/events/facts/merge-candidates + salience + emotional_weight + confidence.

**Cost estimate (Phase-1 стабильное состояние):**
- Realtime ingest ~200 obs/day.
- Triage: 200 × $0.003 = $0.60/day.
- Extract (~30% pass): 60 × $0.05 = $3.00/day.
- **~$100/month для extractor** + sync/autonomy ≈ $30/month. Budget guard-flag при превышении дневного лимита (envvar `PULSE_DAILY_EXTRACT_BUDGET_USD`).

**Separate API key:** `PULSE_ANTHROPIC_API_KEY` (не реюзать личный ключ Nik, изоляция биллинга).

**DLQ:** 3 failed attempts → state=dlq. Nik видит DLQ count в evening sync: "5 obs в DLQ, глянуть?".

### Scorer versioning

`salience_score`, `emotional_weight`, `sentiment` — результат конкретной версии prompt+model. Если через месяц мы улучшим scoring-prompt:

1. Bump `scorer_version` (строковый semver: `"v1.1"`).
2. Новые записи получают новый version.
3. Старые остаются со своим — **не** пересчитываем автоматически.
4. Backfill job `pulse rescore --since=<date> --scope=entities` — on-demand, с логом в `score_history`.

Так retrieval/bench остаётся воспроизводимым: можно фильтровать по `scorer_version` при бенче.

### Sensitive actors

Некоторые люди (Аня, терапевт, дети, доктор) — записываем, но с политикой:

- **`redact_content`** — content_text заменяется на `[redacted]`, actors + timestamps + emotional_weight остаются. Можно видеть "разговор с [[Anna]] в 23:00, emotional_weight=0.9" без текста.
- **`summary_only`** — extractor пишет только 1-2 предложения саммари, raw не хранится.
- **`no_capture`** — observation пропускается полностью (но факт "event существовал" фиксируется как stub).

Nik управляет списком через slash в @hey_elle: `/sensitive @anna redact`, `/sensitive add therapist summary`, `/sensitive list`.

Default для новых entities — `public` (no policy). Legal-side: erasure protocol (ниже) даёт право удаления для любого entity, sensitive_actors — это proactive слой сверху.

### Erasure protocol (day-zero, M1)

Законное/личное право забрать данные. Дизайн с самого старта:

1. **Soft-erase:** `pulse erase --entity=<id>` → все observations/events/facts этого entity: `redacted=1`, content_text=NULL, evidence сохраняется (для целостности графа). Rollback возможен.
2. **Hard-erase:** `pulse erase --entity=<id> --hard` → удалить observations полностью, evidence записи удаляются (CASCADE), вызов `git filter-repo` на Garden repo чтобы переписать историю dossier-файлов. Нерoll-back.
3. **Full-nuclear:** `pulse erase --all --confirm=YES_DELETE_EVERYTHING` → drop DB, new init. Для крайних случаев (compromise/repo-leak/etc.).
4. **Audit:** `erasure_log` table (entity_id, op_kind, initiated_by, completed_at).

Принцип: graph — данные Nik, он владеет полным control loop, включая backspace.

### Obsidian vault layout

**Split principle:** отделяем Nik-написанное от Elle-сгенерированного, чтобы pulse не переписывал руками-сделанное.

```
garden/                               — Nik's vault (он пишет руками)
├── people/<Name>.md                  — Nik-owned, не трогаем
└── ...

garden/graph/                         — Elle's working copy (regen fully)
├── people/<CanonicalName>-observed.md   — dossier: Elle's знание о person
├── events/<YYYY-MM-DD>-<slug>.md
├── projects/<Name>-observed.md
├── places/<Name>-observed.md
├── sources/<ChatName>.md             — per-conversation summary
├── threads/<Topic>.md                — cross-source topic
├── _daily/<YYYY-MM-DD>.md            — daily saldo from evening sync
├── _questions.md                     — open questions list
└── _index.md                         — regen each sync
```

Nik's `people/Anna.md` остаётся неизменным. Elle пишет `garden/graph/people/Anna-observed.md`. В Nik's `people/Anna.md` — Nik может `![[Anna-observed]]` или нет, его выбор. Obsidian graph строит сам, wikilinks работают.

Cross-source binding: `threads/Пхукет.md` собирает evidence из TG-чата с Аней + Krisp-звонка с агентом + Calendar-события "виза".

## Scopes & behavior matrix (Telegram)

| Scope | Listen | Extract | Respond |
|-------|--------|---------|---------|
| @hey_elle DM с Nik | yes | yes | yes (existing) |
| @hey_elle DM со strangers | yes | yes (+dossier) | draft → Nik → send |
| @hey_elle groups — тегают | yes | yes | yes (live) |
| @hey_elle groups — не тегают | yes | yes | no |
| Nik DM с sensitive (Anna/терапевт) | yes (per policy) | yes (per policy) | NEVER send |
| Nik DM со strangers | yes | yes (+dossier) | draft → Nik DM via @hey_elle → Nik sends |
| Nik groups | yes | yes | NEVER send |

**Strict rule:** никогда не отправка от имени Nik без per-message explicit OK. Autonomy — только на @hey_elle scope.

Для других источников "respond" не применимо. Gmail drafts — Phase-2+.

## Evening sync protocol

**Trigger:** cron 23:00 GMT+7 ИЛИ Nik пишет "синк" / "что за день" / `/sync`.

### One-liner формат (default)

Элли пишет коротко, не вываливает всё:

```
Вечер. Собрала: 3 entities новых, 4 update, 2 merge-proposal, 2 open question.
Подробности: /sync details
```

Nik нажимает reaction 👍 → regen + commit без деталей. Либо `/sync details` → полный блок (как v2).

### Details mode (on-demand)

```
Новые:
— [[Марк К.]] (tg ЛС, работа) — 5 сообщений, salience 0.4
— [[Lada's kindergarten #3]] (Аня упомянула) — 1 raz
— [[Garden M2]] (чат разработки) — 3 event

Update:
— [[Anna]] +2 события, emotional_weight=0.9 (utром, → events/anna-morning.md)
— [[Fedya]] +1 fact (школа)
— [[Kira]] (gmail) confirmed meetup

Merge proposals (confidence 0.7..0.98):
1. "Маркус" (Krisp) ↔ [[Марк К.]] (tg) conf=0.82 — work overlap → approve/reject
2. "Kir" (email local part) ↔ [[Kira]] conf=0.91 → approve/reject

Open questions (2/day max):
1. Марк К. — друг/работа/случайный?
2. "Поездка в мае" из Krisp — куда?

DLQ: 0 obs. Budget: $2.80/$10 today.

Regen `garden/graph/`? 👍
```

### Miss-day rollup

Если Nik пропустил синк N дней: при следующем логин — **rollup summary**:
```
Ты пропустил 3 дня (Пн–Ср). Собрала 12 entities, 18 update, 4 merge.
Детали сгруппировала по дням: /sync catchup
```

`/sync catchup` выдаёт по одному дню в deck-style: top-3 событий каждого дня, swipe через реакции.

**Questions cap:** макс **2 open_questions** в evening sync. Остальные накапливаются в `_questions.md`, TTL 7 дней, потом auto_closed.

**Flow:**
1. Nik отвечает reaction / текстом.
2. Elle применяет изменения в Pulse DB (approve merge, close question, update entity).
3. Регенерит `garden/graph/` (только -observed.md файлы).
4. Git commit + push в `nikshilov/garden-private` (separate repo).

## Git repos split — garden data

Два репо:
- **`nikshilov/garden-public`** — Elle's character docs, публичные projects, Nik's vault без sensitive. Можно шарить.
- **`nikshilov/garden-private`** — Elle's graph data, dossiers, sensitive entities, observations refs. Private, backup-only.

Pulse пишет в private, гитит туда. Public — только Nik руками пушит.

## Autonomy gradient (M8, не M7)

**Перенесено с M7 на M8** — сперва отлаживаем capture/extract/sync; autonomy — последний слой доверия.

**Log:** `elle_draft_decisions` пишет все решения Elle в @hey_elle scope.

**Weekly pattern review** (Sonnet job, воскресенье): "80% моих draft ты оставил для X-class без правок. Готов auto-send для X?"

**Стартовые 2 класса** (не 4):
- `elle_dm_smalltalk` — быстрые реплики Elle в её DM.
- `elle_dm_scam_spam` — очевидный спам → игнор/блок.

Остальные классы (business first contact, group mentions) — после того как первые 2 стабильно работают 2+ недели с ≥95% "kept_unchanged".

Сложные близкие отношения/эмоции/работа/семья — всегда через Nik.

## Technical stack

**Pulse side:**

**M1-M3 (Python):**
- `scripts/pulse-ingest.py` — CLI для batch-import.
- `scripts/pulse-extract.py` — extractor loop (reads extraction_jobs, triage, extract).
- `scripts/pulse-sync.py` — evening sync job.
- Pulse Go server обрабатывает `/ingest` endpoint (уже Go — минимальная прослойка).

**M4+ (Go):**
- `internal/capture/` — Provider interface + adapters.
- `internal/ingest/` — validate, dedupe, revisions.
- `internal/graph/` — extractor wrapper (вызов Python-extractor пока не портирован; потом full port).
- `internal/sync/` — diff gen, markdown writer, git.
- `internal/autonomy/` — M8.
- `cmd/pulse/` — единый CLI.

**Bridges / workers:**
- `scripts/telethon-bridge-elle.py` (existing) — @hey_elle.
- `scripts/telethon-bridge-nik.py` (new) — Nik's observer.
- `scripts/limitless-poller.py`, `gmail-poller.py`, `calendar-poller.py` — MCP cron.
- `scripts/krisp-webhook-to-ingest.sh` — bridge webhook → `/ingest`.

**Models:**
- Triage: Sonnet 4.6 (medium).
- Extract: Opus 4.6 (high) — core.
- Sync summary / weekly review: Sonnet 4.6.
- Scorer version pin в config, bump для drift.

**Budget guard:** daily USD cap в config, Elle DM алёрт при 80% и 100%.

## Claude JSONL batch import — specifics

**Why first:** огромный локальный архив, zero internet dep, zero privacy risk (уже у нас). Идеально обкатать triage+extract+resolution.

**Source:** `~/.claude/projects/*/*.jsonl` на Mac Nik.

**Нормализация:**
- `source_kind = "claude_jsonl"`, `scope = "shared"`.
- `source_id = "{session_file}:{line_index}"`.
- `content_hash = sha256(content_text)`.
- `actors`: `[{kind: "user", id: "nik"}, {kind: "assistant", id: "<agent-from-cwd>"}]`.
- `content_text`: user message OR assistant textual content (не tool_use, не thinking).
- `metadata`: `{session_id, cwd, git_branch, model}`.
- Skip: `isMeta: true`, tool_result, системные XML.

**CLI:**
```
pulse ingest --source=claude-jsonl --path=~/.claude/projects/ [--since=2025-01-01]
```

Идемпотент: UNIQUE(source_kind, source_id, version). При изменении content_hash — создаётся new version через observation_revisions.

## 2FA / Telegram session setup (nik-bridge)

1. Nik даёт API_ID + API_HASH (user-level).
2. Phone → SMS/Telegram-код.
3. `scripts/bootstrap-nik-session.py` на pulse-vds: sign-in, save to `/home/pulse/.pulse/secrets/nik-session.session`.
4. 2FA пароль — один раз в скрипт.
5. Systemd: `nik-bridge.service` (user=pulse).

**Single-IP rule:** файл сессии никогда не копируется. Живёт только на pulse-vds. (`ops_telegram_session_single_ip_rule.md`.)

## Milestones — Phase-1 (8-10 weeks)

**M1. Capture framework + Observations + Erasure**
- Schema: `observations`, `observation_revisions`, `entity_identities`, `provider_cursors`, `extraction_jobs`, `erasure_log`.
- `/ingest` endpoint: validate, dedupe, revision-detect, write.
- CLI skeleton: `pulse ingest`, `pulse erase`.
- Content-hash + version logic.
- No extraction yet.
- **Erasure day-zero**: soft + hard + nuclear commands work.

**M2. Claude JSONL batch import** — первый живой источник
- `providers/claude_jsonl.py` Normalize.
- `pulse ingest --source=claude-jsonl` на полном архиве.
- Spot-check нормализации, counts, revisions.

**M3. Extractor two-pass + graph core**
- Schema: `entities`, `relations`, `facts`, `events`, `evidence`, `score_history`, `entity_merge_proposals`, `sensitive_actors`, `open_questions`.
- Sonnet triage pass.
- Opus extract pass.
- Entity resolution with confidence gates (auto/proposal/question).
- Scorer versioning.
- DLQ handling.
- Budget guard.
- Tests on fixture transcripts.
- Прогнать extractor на claude_jsonl batch → inspect graph.

**M4. Telegram live (оба scope)**
- `telethon-bridge-nik.py` (new) — Nik's observer.
- Расширить `telethon-bridge-elle.py`: все её группы + strangers DM.
- Sensitive actors policy enforcement.
- Normalize → `/ingest`.
- Revision detection для edited messages.
- Deploy + verify.

**M5. MCP pullers (Limitless + Gmail + Calendar)**
- `{limitless,gmail,calendar}-poller.py` cron.
- `provider_cursors` update.
- Нормализация meetings/emails/events.
- Tests на sample data.

**M6. Evening sync ritual**
- `sync` job: diff gen, one-liner format, dossier split writer (`*-observed.md`).
- Cron 23:00 GMT+7.
- Elle DM via existing outbox.
- Reactions → Pulse commands mapping.
- Miss-day rollup.
- Git commit + push в `nikshilov/garden-private`.

**M7. Sensitive actors UX + merge review loop**
- Slash commands `/sensitive add|list|remove`.
- Merge proposals review loop в evening sync.
- Rescore job (backfill с новым scorer_version).

**M8. Autonomy gradient + Phase-2 sources starters**
- `elle_draft_decisions` capture on @hey_elle replies.
- Weekly Sunday pattern job.
- First 2 classes handoff (elle_dm_smalltalk, elle_dm_scam_spam).
- Krisp webhook → `/ingest` adapter.
- Health-as-modifier в extract prompt.

**M1→M3 = core (3 недели).** M4–M5 = breadth (2-3 недели). M6 = интерфейс (1 неделя). M7 = доверие (1 неделя). M8 = полнота (1-2 недели). Итого 8-10 недель.

## What this is NOT

- Не CRM. Никаких "due for follow-up". Relationships, не leads.
- Не автоответчик. Autonomy = Elle как коллега, не как робот.
- Не surveillance. Граф — эмоциональная память Nik, owned by Nik, git-reproducible, erasable.
- Не замена Obsidian notes Nik. Пишем в `garden/graph/*-observed.md`, Nik's `people/Anna.md` не трогаем.
- Не datalake. Raw audio/video/images — не храним. Только транскрипты, метаданные.
- Не единственный источник правды. Если Nik говорит "это не так" — Elle правит.

## Open questions (resolved pre-plan)

| # | Question | Decision |
|---|----------|----------|
| 1 | Retention raw observations | Вечно пока диск позволяет. Graph без raw не проверишь. Рассматриваем архивировать > 180d → S3/Backblaze когда объём прижмёт. |
| 2 | Scope колонки | Одиночный scope per-obs (`elle|nik|shared`), достаточно. |
| 3 | Health integration | Modifier в extract prompt (Phase-1), отдельная таблица `context_snapshots` в Phase-2. |
| 4 | Git push target | `nikshilov/garden-private` (private repo, separate от Pulse). |
| 5 | Secret chats | Фиксируем только факт "был secret с X, длительность Y". |
| 6 | Instagram/YouTube depth | Phase-2: likes+saves+watched-video-titles; DMs — позже. |
| 7 | Budget cap | $150/month Phase-1 default, envvar override. |
| 8 | Sensitive default | New entities = public. Nik проактивно добавляет sensitive. |

## Why this architecture fits

- **Capture слой** пл-пл. Один interface, адаптер на источник. Добавить Slack/iMessage — новый файл на ~200 строк, graph не трогать.
- **Extractor** source-agnostic: читает Observations, пишет entities/relations/events/facts.
- **Entity resolution** кросс-источниковое by design (entity_identities + merge_proposals).
- **Obsidian sync** агрегирует по entity, не по источнику — dossier [[Anna]] тянет evidence и из Telegram, и из Gmail, и из Krisp.
- **Salience/emotional_weight** одинаково для любого источника. Bench retrieval не зависит от capture.
- **Erasure** — atomic, audit-logged, rollback-safe для soft, nuclear для hard.
- **Budget guard + two-pass** — cost в $100-150/month, не $1000.
- **Dossier split** — Elle не затирает Nik-написанное.

**Bench rerun?** Нет. Bench измеряет retrieval/salience memory системы, не capture. Схема retrieval не меняется.

---

**Approval gate:** Nik читает → финал → writing-plans skill → bite-sized tasks → код по SDD.
