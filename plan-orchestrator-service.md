# Детальный план реализации: Orchestrator Service

> Часть общей архитектуры — см. [architecture.md](architecture.md) (диаграмма, общие конвенции, топология репозитория). Зависит от [`plan-data-service.md`](plan-data-service.md), [`plan-parser-service.md`](plan-parser-service.md) и [`plan-messaging-service.md`](plan-messaging-service.md) — Оркестратор реально вызывает все три сервиса, тесты на моках трёх downstream-клиентов, но e2e-верификация требует все три поднятыми и живыми.

Фаза 4 общего порядка исполнения (см. таблицу в architecture.md) — реализуется последним из четырёх сервисов, перед общей интеграцией (Фаза 5).

## Context

Оркестратор — единственная точка входа для пользователя, тонкий координатор поверх уже специфицированных Data Service / Parser / Messaging. Своего хранилища кампаний не заводит — кампании уже живут в Data Service (`CRUD /campaigns`).

Общая структура каталога и конвенции (config, clients/*, /health, обычный Dockerfile без Playwright) — см. раздел "Общие конвенции для Парсера/Отправки/Оркестратора" в [architecture.md](architecture.md). Ниже — специфика именно Orchestrator Service.

## Структура проекта

```
orchestrator-service\
├── app\
│   ├── main.py
│   ├── config.py                  # DATA_SERVICE_URL, PARSER_SERVICE_URL, MESSAGING_SERVICE_URL, POLL_INTERVAL_SEC=5, POLL_TIMEOUT_SEC=1800, LOG_LEVEL
│   ├── logging_conf.py
│   ├── clients\
│   │   ├── data_service.py         # create_campaign, get_campaign, update_campaign_status, list_leads(campaign_id,status), cooldown_account (для /pause)
│   │   ├── parser_service.py        # start_parse(...) -> task_id; get_parse_status(task_id)
│   │   └── messaging_service.py      # start_send(campaign_id); get_send_status(campaign_id)
│   ├── schemas\campaign.py           # CampaignCreate, CampaignStartResponse, CampaignStatusOut{campaign, phase, stats}
│   ├── orchestration.py               # in-memory OrchestrationTaskStore + run_campaign_flow(campaign_id)
│   ├── routers\
│   │   ├── campaigns.py                # POST /campaigns, POST /campaigns/{id}/start, GET /campaigns/{id}
│   │   └── accounts.py                  # POST /accounts/{id}/pause
│   └── exceptions.py
├── tests\ (respx-моки трёх downstream-клиентов; тест state-машины orchestration.py: parsing→messaging→done/failed/timeout; тест агрегации stats)
├── scripts\entrypoint.sh
├── Dockerfile              # обычный, без Playwright
├── requirements.txt
├── .env.example
└── README.md
```

## Логика

`POST /campaigns` не заводит собственного хранилища — кампании уже живут в Data Service (`CRUD /campaigns`). Оркестратор — тонкий прокси: `data_service_client.create_campaign(payload)`, возвращает результат как есть.

`POST /campaigns/{id}/start` → `run_campaign_flow` через `BackgroundTasks` (тот же паттерн in-memory-задачи, что и в Parser/Messaging — для консистентности между тремя не-БД сервисами):

1. Проверка кампании через Data Service (`GET /campaigns/{id}`).
2. Обновление статуса кампании на `running`.
3. `parser_client.start_parse(platform, keyword, filters, campaign_id)` → `task_id`.
4. Поллинг `GET /parse/{task_id}/status` каждые `POLL_INTERVAL_SEC` до `done`/`failed`/таймаута `POLL_TIMEOUT_SEC`.
5. Если `done` → `messaging_client.start_send(campaign_id)` → аналогичный поллинг `GET /campaigns/{id}/send-status`.
6. Если оба этапа `done` → статус кампании `completed`.
7. При ошибке/таймауте на любом этапе → статус кампании выставляется в `paused` (единственный подходящий терминал из уже принятого enum `draft/running/paused/completed` — там нет `failed`), реальная причина сбоя сохраняется только в in-memory `OrchestrationTask` и отдаётся через `GET /campaigns/{id}`.

`GET /campaigns/{id}` сливает: (а) текущее состояние `OrchestrationTask` из памяти (если flow запускался), (б) статистику из Data Service — для каждого `status` в `new/queued/contacted/replied/rejected` берётся `len(GET /leads?campaign_id=&status=X&limit=1000)`. Это приближение (в API Data Service нет `COUNT`-эндпоинта), приемлемо при MVP-объёмах (десятки–сотни лидов).

`POST /accounts/{id}/pause` — тонкий прокси к уже существующему `POST /accounts/{id}/cooldown` (например `{minutes: <большое число>}` или `{permanent: true}`) — новый эндпоинт в Data Service не создаётся.

Концурентность/поллинг — без RabbitMQ/Redis, только REST + polling. Дефолты: `POLL_INTERVAL_SEC=5`, `POLL_TIMEOUT_SEC=1800` (30 минут — разумный потолок для парсинга/рассылки по одной MVP-кампании), оба вынесены в `.env`.

## Verification

1. `docker compose up --build` (standalone, `DATA_SERVICE_URL`/`PARSER_SERVICE_URL`/`MESSAGING_SERVICE_URL` указывают на уже поднятые сервисы) — `GET http://localhost:8004/health` → `{"status":"ok"}`.
2. `POST /campaigns` создаёт кампанию в Data Service (проверить `GET /campaigns/{id}` там напрямую).
3. `POST /campaigns/{id}/start` при живых Parser+Messaging (в `VK_ADAPTER_MODE=fake`) доводит кампанию до `status=completed`; `GET /campaigns/{id}` по пути показывает промежуточные фазы (`parsing`→`messaging`→`done`).
4. Таймаут/ошибка на любом этапе (например Parser недоступен) → кампания уходит в `paused`, причина видна в `GET /campaigns/{id}`.
5. `POST /accounts/{id}/pause` реально переводит аккаунт в cooldown/banned в Data Service.
6. `pytest` из `orchestrator-service/` — тесты на моках трёх клиентов (respx), включая state-машину `parsing→messaging→done/failed/timeout` и агрегацию stats.

## Следующие шаги

После зелёной верификации — Фаза 5 (интеграция всех 4 сервисов через корневой `docker-compose.yml` + e2e smoke) — см. раздел "Интеграционная верификация" в [architecture.md](architecture.md).
