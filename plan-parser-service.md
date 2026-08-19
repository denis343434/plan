# Детальный план реализации: Parser Service

> Часть общей архитектуры — см. [architecture.md](architecture.md) (диаграмма, интерфейс адаптера, общие конвенции, топология репозитория, антибан-логика VK). Зависит от [`plan-data-service.md`](plan-data-service.md) — Data Service должен быть доведён до зелёного состояния (Фаза 0) прежде чем писать код Парсера: Parser реально дёргает его REST API в тестах (пусть частично на моках), контракт должен существовать не только на бумаге.

Фазы 1–2 общего порядка исполнения (см. таблицу в architecture.md).

## Context

Data Service полностью специфицирован (схема, эндпоинты, тесты, Docker) — единственная точка правды по данным. Parser общается с ним только через REST API, ничего не переизобретая из уже принятых там решений (UUID PK, VARCHAR+CHECK вместо нативных enum, атомарная `/accounts/next-available` через `FOR UPDATE SKIP LOCKED`, Fernet-шифрование сессий).

Общая структура каталога и конвенции (config, clients/data_service.py, /health, Dockerfile-паттерн) — см. раздел "Общие конвенции для Парсера/Отправки/Оркестратора" в [architecture.md](architecture.md). Ниже — специфика именно Parser Service.

## Структура проекта

```
parser-service\
├── app\
│   ├── main.py
│   ├── config.py              # DATA_SERVICE_URL, VK_ADAPTER_MODE(fake|playwright, default fake), VK_HEADLESS, MIN_DELAY_SEC, MAX_DELAY_SEC, LOG_LEVEL
│   ├── logging_conf.py
│   ├── clients\data_service.py  # bulk_insert_leads, next_available_account, get_session(account_id), release_account, cooldown_account
│   ├── schemas\parse.py          # ParseRequest{platform, keyword, filters, campaign_id}; ParseTaskOut{task_id, status, found, inserted, skipped, error}
│   ├── adapters\
│   │   ├── base.py                # SourceAdapter Protocol (как определено в architecture.md), RawLead, ParseFilters
│   │   ├── vk.py                   # VkParserAdapter — playwright.sync_api
│   │   ├── vk_fake.py                # FakeVkAdapter — детерминированный набор RawLead без браузера
│   │   ├── stub.py                    # NotImplementedAdapter — tg/instagram
│   │   └── registry.py                 # get_adapter(platform) -> SourceAdapter, учитывает VK_ADAPTER_MODE
│   ├── tasks.py                          # in-memory TaskStore + run_parse_task()
│   ├── routers\parse.py                    # POST /parse, GET /parse/{task_id}/status
│   └── exceptions.py
├── tests\
│   ├── conftest.py
│   ├── test_adapters_registry.py           # NotImplementedAdapter → [] без исключений; регистрация fake/playwright режима
│   ├── test_task_status_transitions.py     # queued→running→done/failed на FakeVkAdapter
│   └── test_parse_endpoint_mocked.py       # respx мокает Data Service, проверяет payload /leads/bulk
├── scripts\
│   ├── entrypoint.sh
│   └── vk_manual_login.py                  # разовый ручной логин (headful), storage_state → PUT /accounts/{id}/session
├── Dockerfile
├── requirements.txt
├── .env.example
└── README.md
```

## Хранение статуса задач

**In-memory `dict[UUID, ParseTask]` + FastAPI `BackgroundTasks`.** Не просто «проще» — фактически единственный вариант, совместимый со стеком: Postgres только у Data Service, а MVP-порядок явно разрешает поллинг без брокера. Ограничение (осознанно принимается): состояние теряется при рестарте контейнера, задачи не шарятся между репликами — для MVP сервис работает в одном экземпляре, распараллеливание идёт на уровне VK-аккаунтов (уже решено на стороне Data Service через `next-available`), а не реплик Parser-сервиса.

## Логика

- `POST /parse` — создаёт `task_id=uuid4()`, статус `queued`, `background_tasks.add_task(run_parse_task, ...)`, отвечает `202 {task_id, status: "queued"}` немедленно.
- `run_parse_task` — `status=running` → `registry.get_adapter(platform)` → если `vk`: `next_available_account(platform=vk, purpose=parsing, task_ref=task_id)` (409 → задача `failed`, `error="no account available"`) → `get_session(account_id)` → адаптер `search_communities(keyword, filters)` → батчами (например по 200) `bulk_insert_leads` → `release_account`. При обнаружении капчи/флуда внутри `vk.py` — `cooldown_account(account_id, minutes=..., reason="captcha_detected")`, задача завершается `failed`, но уже собранные лиды всё равно отправляются в `/leads/bulk` до прерывания (частичный результат не теряется).
- `GET /parse/{task_id}/status` → `ParseTaskOut` или `404`, если `task_id` неизвестен.

## Тесты — что можно/нельзя без реального VK-логина

- Автоматически (CI): интерфейс адаптера, `NotImplementedAdapter`, переходы статусов задачи, `POST /parse` целиком на `FakeVkAdapter` + замоканном `DataServiceClient` (через `respx`).
- Вручную/вне CI: реальный DOM-скрейпинг живого VK (селекторы поддерживаются вручную), точность детекции капчи, сам факт логина — для него `scripts/vk_manual_login.py` (headful Playwright, ручной ввод логина/пароля/2FA один раз, дамп `context.storage_state()`, `PUT` в Data Service).

## Verification

1. `docker compose up --build` (standalone, `DATA_SERVICE_URL=http://localhost:8001` против уже поднятого Data Service) — `GET http://localhost:8002/health` → `{"status":"ok"}`.
2. `POST /parse` с `VK_ADAPTER_MODE=fake` — задача проходит `queued→running→done`, лиды реально появляются в Data Service (`GET /leads?campaign_id=...`), аккаунт после задачи снова `active` (не завис в `locked`).
3. `POST /parse` при отсутствии свободных аккаунтов (все залочены/в cooldown) → задача завершается `failed`, `error="no account available"`.
4. `pytest` из `parser-service/` — все 3 тестовых файла проходят (моки, без реального VK и без реального Data Service).

## Следующие шаги

После зелёной верификации — Фаза 3: [`plan-messaging-service.md`](plan-messaging-service.md).
