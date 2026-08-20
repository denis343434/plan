# VK Lead-Gen — 4 микросервиса

Оркестратор (API Gateway) + Парсер + Отправка сообщений + БД (Data Service).
Полная архитектура, контракты между сервисами и антибан-логика VK — [`architecture.md`](architecture.md).
Пошаговые планы реализации каждого сервиса — `plan-*.md`.

```
Оркестратор (8004)  ──REST──►  Парсер (8002)
       │                            │
       ├───────────REST─────────────┤
       ▼                            ▼
   Отправка (8003) ◄──REST──►  БД / Data Service (8001) ──► Postgres (5433)
```

## Фаза 5 — интеграция (этот документ)

Данный `docker-compose.yml` — единственный корневой стек, поднимающий все 4 сервиса + Postgres
одной командой (см. раздел "Стек и запуск" в `architecture.md`). У Data Service есть ещё и
собственный `data-service/docker-compose.yml` — он остаётся для изолированной разработки этого
сервиса и тестов, к общей интеграции отношения не имеет.

## Быстрый старт

```bash
docker compose up --build
```

Дефолты (dev-ключ шифрования, VK_ADAPTER_MODE=fake и т.д.) уже прописаны в `docker-compose.yml` —
`.env` не обязателен. Хотите переопределить что-то — `cp .env.example .env` и правьте.

Поднимутся:

| Сервис | URL | Назначение |
|---|---|---|
| postgres | localhost:5433 | только для data-service |
| data-service | http://localhost:8001 | БД поверх REST |
| parser-service | http://localhost:8002 | парсинг (VK_ADAPTER_MODE=fake — без браузера) |
| messaging-service | http://localhost:8003 | рассылка (VK_ADAPTER_MODE=fake — DryRunAdapter) |
| orchestrator-service | http://localhost:8004 | единая точка входа |

Проверка живости:

```bash
curl http://localhost:8001/health
curl http://localhost:8002/health
curl http://localhost:8003/health
curl http://localhost:8004/health
# везде {"status":"ok"}
```

## E2E smoke-флоу

В `VK_ADAPTER_MODE=fake` парсер отдаёт 3 детерминированных лида без браузера, а отправка
работает через `DryRunAdapter` (логирует "would send", не открывает браузер) — весь путь
campaign → parse → send → completed проверяется без реального VK-аккаунта. Но обе стадии
всё равно проходят через настоящую locking-логику Data Service (`next-available` / `release` /
`cooldown`), поэтому один VK-аккаунт в пуле нужен реально — без него `next-available` вернёт 409
и кампания застрянет в `waiting_for_account`/`paused`.

### 1. Завести VK-аккаунт (purpose=both — обслуживает и парсинг, и рассылку)

```bash
curl -s -X POST http://localhost:8001/accounts -H "Content-Type: application/json" -d '{
  "platform": "vk",
  "login": "smoke-test-account",
  "purpose": "both",
  "hourly_limit": 100,
  "daily_limit": 1000
}'
# → {"id": "<account_id>", "status": "active", ...}
```

### 2. Записать сессию аккаунта (обязателен даже в fake-режиме — Parser реально проверяет наличие сессии)

```bash
curl -s -X PUT http://localhost:8001/accounts/<account_id>/session -H "Content-Type: application/json" -d '{
  "storage_state": {"cookies": [], "origins": []}
}'
```

Без этого шага `parser-service` завершится с ошибкой `session for account ... not found` — `FakeVkAdapter`
не открывает браузер, но всё равно проходит через настоящий `get_session` в Data Service (см.
"Интеграционная верификация" в `architecture.md`).

### 3. Создать кампанию (через Оркестратор — единственная точка входа)

```bash
curl -s -X POST http://localhost:8004/campaigns -H "Content-Type: application/json" -d '{
  "name": "Smoke test",
  "platform": "vk",
  "keyword": "test"
}'
# → {"id": "<campaign_id>", "status": "draft", ...}
```

### 4. Создать шаблон для этой кампании (напрямую в Data Service — CRUD /templates не проксируется через Оркестратор)

```bash
curl -s -X POST http://localhost:8001/templates -H "Content-Type: application/json" -d '{
  "campaign_id": "<campaign_id>",
  "variant": "A",
  "body": "Здравствуйте, {{org_name}}!"
}'
```

Без шаблона `messaging-service` завершит рассылку с `error="no template configured for campaign"`.

### 5. Запустить кампанию

```bash
curl -s -X POST http://localhost:8004/campaigns/<campaign_id>/start
# → 202 {"campaign_id": "...", "phase": "idle"}
# "idle" — начальная фаза новой задачи в момент самого ответа; run_campaign_flow
# запускается в BackgroundTasks сразу после и переводит phase в parsing/messaging асинхронно.
```

### 6. Опросить статус до завершения

```bash
watch -n2 curl -s http://localhost:8004/campaigns/<campaign_id>
```

`phase` проходит `parsing → messaging → done`. Итоговый ответ:

```json
{
  "campaign": {"...": "...", "status": "completed"},
  "phase": "done",
  "error": null,
  "stats": {"new": 0, "queued": 0, "contacted": 3, "replied": 0, "rejected": 0}
}
```

Если `phase` = `failed`/`timeout` — причина в поле `error`, а сама кампания в Data Service
переведена в `status=paused` (см. "Терминальный статус кампании при сбое" в `architecture.md`).

### 7. Проверить результат напрямую в Data Service

```bash
curl "http://localhost:8001/leads?campaign_id=<campaign_id>&status=contacted"
# → 3 fake-лида (fake-test-0/1/2), у каждого status=contacted

curl "http://localhost:8001/messages?account_id=<account_id>"
# → 3 записи с delivery_status=sent
```

## Известные ограничения

- `VK_ADAPTER_MODE=playwright` (реальный VK) в этом smoke-флоу не проверяется — нужен
  залогиненный аккаунт, см. `parser-service/scripts/vk_manual_login.py` /
  `messaging-service/scripts/vk_manual_login.py`.
- Стадии `parsing`/`messaging` опрашиваются поллингом (`POLL_INTERVAL_SEC`/`POLL_TIMEOUT_SEC`
  в `orchestrator-service`), без брокера очередей — соответствует MVP-решению в `architecture.md`.

## Локальная разработка отдельных сервисов

Каждый сервис самостоятельно runnable и тестируем без остальных — см. `README.md` внутри
`data-service/`, `parser-service/`, `messaging-service/`, `orchestrator-service/`.
