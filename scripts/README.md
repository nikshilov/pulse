# Pulse Scripts — Knowledge Graph Pipeline

Pipeline для построения персонального knowledge graph из наблюдений (чаты, голосовые, заметки).

## Архитектура

```
observations → ingest → extraction_jobs → extract → graph (entities/relations/events/facts)
                                                        ↓
                                            consolidate (dedup, decay, gaps)
                                                        ↓
                                            retrieve (keyword BFS → ranked context)
```

## Компоненты

### pulse_ingest.py — Загрузка наблюдений

Batch-import наблюдений в Pulse из локальных источников.

```bash
python pulse_ingest.py --source claude-jsonl --path ~/chat-exports/ --since 2026-01-01
```

Провайдеры:
- `claude-jsonl` — парсит Claude JSONL экспорты (`providers/claude_jsonl.py`)

### pulse_extract.py — Двухпроходная экстракция

Читает `extraction_jobs` (state=pending), запускает два прохода:

1. **Sonnet triage** — классифицирует наблюдения: `extract` / `skip` / `defer`
2. **Opus extract** — извлекает entities, relations, events, facts через tool-use

```bash
# Один прогон (до 10 pending jobs)
python pulse_extract.py --db ~/pulse.db

# С лимитом бюджета
python pulse_extract.py --db ~/pulse.db --budget 5.0
PULSE_DAILY_EXTRACT_BUDGET_USD=2.0 python pulse_extract.py --db ~/pulse.db
```

Особенности:
- **Checkpoint/resume**: triage и extract артефакты сохраняются в `extraction_artifacts`. При crash — продолжает с последнего checkpoint
- **SAVEPOINT per item**: ошибка в одном entity/event/relation/fact не ломает остальные
- **DLQ**: после 3 неудачных попыток job уходит в `dlq` (dead letter queue)
- **Метрики**: input/output tokens записываются в `extraction_metrics`

### pulse_consolidate.py — Обогащение графа

Периодическая консолидация: чистка, обогащение, метрики.

```bash
python pulse_consolidate.py --db ~/pulse.db
```

Что делает:
- **Salience decay** — экспоненциальное затухание: `score × exp(-λ × days)`, λ зависит от kind (person=0.001, concept=0.01). Всегда запускается, не зависит от skip-guard
- **Skip-guard** — пропускает полную консолидацию если нет новых entities/evidence/events
- **Dedup** — находит кандидатов на слияние (SequenceMatcher ≥ 0.8)
- **Approved merges** — выполняет слияния из `entity_merge_proposals` (state=approved)
- **Co-occurrence** — находит пары entities без relation, но появляющиеся в ≥3 общих events
- **Knowledge gaps** — entities с ≥3 mentions но ≤1 fact → auto-populate `open_questions`
- **Stale questions** — закрывает вопросы с истёкшим TTL
- **Observability** — valence trend (sentiment rolling avg), extraction efficiency (entities/1K tokens)

### extract/ — Модули экстракции

| Файл | Назначение |
|------|-----------|
| `prompts.py` | Промпты для triage (Sonnet) и extraction (Opus) |
| `tool_schemas.py` | JSON-схемы для tool-use API (triage_observations, save_extraction) |
| `resolver.py` | Entity resolution: exact match → bind, 0.7-0.98 → proposal, <0.7 → new + question |
| `scorer.py` | Нормализация salience/emotional_weight/sentiment с version pinning |
| `retrieval.py` | Keyword-based граф-retrieval с BFS expansion и hop penalty |

### extract/retrieval.py — Контекстный поиск

BFS-expansion от seed entities с ранжированием.

```python
from extract.retrieval import retrieve_context
result = retrieve_context(con, "Как дела у Ани?", top_k=10, depth=2)
# result = {matched_entities: [...], total_matched: 4, retrieval_method: "keyword", max_depth_used: 2}
```

Ранжирование (Garden-style, победитель 9-way empathic bench, Apr 2026):
`score = (salience + emotional_weight) × recency × anchor × hop_penalty`
- `recency = exp(-λ × days_since_last_seen)` — λ зависит от kind (`DECAY_RATES`):
  person=0.001 (t½≈693d), place=0.003, project=0.005, concept=0.01 (t½≈69d). Decay только на retrieval, в DB не мутирует
- `anchor = 1.5` для persons с `emotional_weight > 0.6` (Anna/Sonya/Kristina-class), иначе 1.0 — поднимает эмоционально центральных людей
- `emotional_weight` аддитивно к salience, не множительно — salience=0.3 emo=0.9 не теряется к salience=0.9 emo=0.0
- `hop_penalty = 0.7 ^ hop` — прямое совпадение → 1.0, через 1 hop → 0.7, через 2 → 0.49

### providers/ — Адаптеры источников

- `claude_jsonl.py` — парсит Claude JSONL экспорты, нормализует в observations

## Миграции

SQLite миграции в `internal/store/migrations/`:

| # | Файл | Что создаёт |
|---|------|------------|
| 001 | outbox.sql | Outbox для Telegram |
| 002 | context.sql | Контекст |
| 003 | observations.sql | observations, observation_revisions, ingest_cursors, erasure_log |
| 004 | extraction_jobs.sql | extraction_jobs |
| 005 | graph_core.sql | entities, relations, events, facts, evidence, merge_proposals, open_questions |
| 006 | event_entities.sql | event_entities junction + extraction_artifacts |
| 007 | metrics.sql | extraction_metrics |
| 008 | consolidation.sql | consolidation_metadata + open_questions dedup index |

## Тесты

```bash
# Все тесты
python -m pytest scripts/tests/ -q

# Конкретный модуль
python -m pytest scripts/tests/test_retrieval.py -v
python -m pytest scripts/tests/test_consolidate.py -v
python -m pytest scripts/tests/test_extract_loop.py -v
```

98 тестов (на момент Phase 2e). E2E тесты с реальным Anthropic API пропускаются без `ANTHROPIC_API_KEY`.

## Модели

| Проход | Модель | Назначение |
|--------|--------|-----------|
| Triage | claude-sonnet-4-6 | Быстрая классификация ($3/$15 per 1M tokens) |
| Extract | claude-opus-4-6 | Глубокая экстракция ($15/$75 per 1M tokens) |

## Переменные окружения

| Переменная | Описание | Default |
|-----------|----------|---------|
| `ANTHROPIC_API_KEY` | Ключ для Sonnet/Opus | required |
| `PULSE_DAILY_EXTRACT_BUDGET_USD` | Дневной лимит на extraction | 10.0 |
| `PULSE_KEY` | X-Pulse-Key для ingest API | — |
| `PULSE_URL` | URL Pulse сервера | http://localhost:18789 |
