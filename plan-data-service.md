# Детальный план реализации: БД-сервис (Data Service)

> Часть общей архитектуры — см. [architecture.md](architecture.md). Краткий обзор таблиц/API этого сервиса — там же, раздел "2. БД (Data Service)". Этот файл — полный план реализации.

Реализуется в `E:\plan\data-service`, самостоятельно runnable и тестируемый (`docker compose up`, `pytest`) без реальных Парсера/Отправки/Оркестратора — они появятся позже как соседние директории (`parser-service`, `messaging-service`, `orchestrator-service`).

Это первый из 4 сервисов в общем порядке исполнения (Фаза 0) — блокирует всё остальное: Parser/Messaging/Orchestrator реально дёргают его REST API (пусть частично на моках), контракт должен существовать не только на бумаге.

## Технические решения

| Вопрос | Решение | Почему |
|---|---|---|
| PK | `UUID`, генерируется в Python (`default=uuid4`) | не нужен `pgcrypto`; id известен до INSERT |
| Enum-поля (`status`, `purpose`, `platform`...) | `VARCHAR` + `CHECK (...)` в БД, `StrEnum` в Python | нативный Postgres ENUM тяжело мигрировать; CHECK меняется обычной alembic-ревизией |
| `accounts.status` | `active / flood / banned / cooldown / locked` — добавлен `locked` сверх исходного списка | нужен явный "занят" статус между `next-available` и `release` |
| Шифрование `sessions.storage_state` | Fernet (`cryptography`), ключ — `SESSION_ENCRYPTION_KEY` в `.env`, колонка `storage_state_enc BYTEA` | явное требование — сессии хранятся зашифрованными |
| ORM/драйвер | SQLAlchemy 2.0 sync + `psycopg` v3 | `FOR UPDATE SKIP LOCKED` и транзакции проще контролировать синхронно; для размера сервиса async не даёт выгоды |
| Миграции | Alembic, одна первая ревизия на все 6 таблиц | схема стабильна на старте |
| Rate-limit (`hourly_limit`/`daily_limit`) | считается на лету агрегатом по `messages`, не денормализуется | единственный источник правды |
| Протухшие locks | `accounts.locked_until` — если `status='locked' AND locked_until < now()`, аккаунт снова доступен | защита от воркера, упавшего без вызова `/release` |

## Структура проекта

```
E:\plan\data-service\
├── app\
│   ├── main.py                 # FastAPI() + роутеры + /health + exception handlers
│   ├── config.py                # Settings(BaseSettings) из .env
│   ├── database.py              # engine, SessionLocal, get_db()
│   ├── enums.py                 # Platform, LeadStatus, CampaignStatus, AccountPurpose, AccountStatus, DeliveryStatus, ReplyStatus
│   ├── security.py              # Fernet encrypt/decrypt для storage_state
│   ├── exceptions.py            # NotFoundError, NoAccountAvailableError -> HTTPException
│   ├── models\                  # SQLAlchemy: base.py, lead.py, campaign.py, account.py, session.py, message.py, template.py
│   ├── schemas\                 # Pydantic v2 *Create/*Update/*Out — по одному файлу на сущность
│   ├── crud\                    # leads.py, campaigns.py, accounts.py, sessions.py, messages.py, templates.py
│   └── routers\                 # по одному файлу на сущность, тонкие — вызывают crud/*
├── alembic\
│   ├── env.py                   # target_metadata = Base.metadata, url из app.config
│   └── versions\0001_initial_schema.py
├── alembic.ini
├── tests\
│   ├── conftest.py               # testcontainers Postgres, alembic upgrade head, db_session с TRUNCATE между тестами, TestClient
│   ├── test_leads_bulk_dedup.py
│   ├── test_accounts_next_available_concurrency.py
│   ├── test_accounts_lock_release_cooldown.py
│   ├── test_messages_and_rate_limit.py
│   └── test_templates_campaigns_crud.py
├── scripts\entrypoint.sh         # wait-for-db + alembic upgrade head + exec uvicorn
├── Dockerfile
├── docker-compose.yml            # db (postgres:16-alpine) + data-service
├── requirements.txt
├── .env.example
├── .dockerignore / .gitignore
└── README.md
```

## Схема БД (6 таблиц)

- **leads**: `id, platform, external_id, group_url, admin_contact?, title?, status, campaign_id?, found_at`. `UNIQUE(platform, external_id)` + `ON CONFLICT DO NOTHING`. `CHECK status IN (new,queued,contacted,replied,rejected)`. Индекс `(status, platform, campaign_id)`.
- **campaigns**: `id, name, platform, keyword, template_id?, status, created_at`. `CHECK status IN (draft,running,paused,completed)`.
- **accounts**: `id, platform, login (unique), purpose, proxy?, user_agent?, viewport?, hourly_limit, daily_limit, status, warmup_stage, cooldown_until?` + добавленные `locked_until?, locked_task_ref?, last_used_at?`. `CHECK purpose IN (parsing,messaging,both)`, `CHECK status IN (active,flood,banned,cooldown,locked)`. Индекс `(status, platform, purpose)`.
- **sessions**: `id, account_id (FK, unique — 1:1), storage_state_enc (BYTEA, Fernet), updated_at`.
- **messages**: `id, lead_id (FK), account_id (FK), template_variant?, text_sent, sent_at, delivery_status, reply_status`. Индекс `(account_id, sent_at)` — критичен для rate-limit агрегата.
- **templates**: `id, campaign_id? (FK), variant, body`.

`campaigns.template_id` и `templates.campaign_id` — логический цикл ссылок, оба nullable; в миграции создаются без FK друг на друга, FK добавляются отдельными `op.create_foreign_key` в конце ревизии.

## Эндпоинты

- `POST /leads/bulk` — вставка через `pg_insert(...).on_conflict_do_nothing(index_elements=["platform","external_id"]).returning(Lead.id)`, ответ `{inserted, skipped, lead_ids}`.
- `GET /leads?status=&platform=&campaign_id=&limit=&offset=`
- `PATCH /leads/{id}/status`
- `POST /accounts` (создание в пуле), `GET /accounts?status=&platform=&purpose=` (с вычисляемыми `hourly_used`/`daily_used` через join по `messages`)
- **`POST /accounts/next-available`** — `{platform, purpose, lock_ttl_seconds=900, task_ref?}` → 200 `AccountOut` или 409 если ничего не свободно
- `POST /accounts/{id}/lock`, `/release`, `/cooldown` (`{minutes, permanent=false, reason?}`)
- `POST /messages`, `GET /messages?lead_id=&account_id=`
- `PUT /accounts/{id}/session` (принимает сырой Playwright `storage_state`, шифрует), `GET /accounts/{id}/session` (расшифровывает)
- CRUD `/templates`, `/campaigns`

## Атомарная `/accounts/next-available` — ключевая логика

В `app/crud/accounts.py`, одна транзакция:
1. `SELECT` кандидатов (`platform`, `purpose IN (purpose, 'both')`, `status='active' OR (status='locked' AND locked_until < now())`, не в cooldown), `ORDER BY warmup_stage, last_used_at NULLS FIRST`, `LIMIT 20`, **`.with_for_update(skip_locked=True)`** — это и есть требуемая атомарность (`FOR UPDATE SKIP LOCKED`), исключает гонку между параллельными воркерами.
2. Если кандидатов нет → `NoAccountAvailableError` → HTTP 409.
3. Считаем `hourly`/`daily` использование по `messages` за последний час/сутки для этих кандидатов, отсеиваем тех, кто упёрся в лимит.
4. Первый прошедший — переводим в `status='locked'`, `locked_until=now()+lock_ttl`, `locked_task_ref=task_ref`, `last_used_at=now()`, коммит.
5. Если после фильтра по лимитам никого не осталось → тоже 409.

`lock`/`release`/`cooldown` — прямые CRUD-операции с `SELECT ... FOR UPDATE` на одну строку по id.

## Docker

- `Dockerfile`: `python:3.12-slim`, `pip install -r requirements.txt`, `entrypoint.sh`.
- `entrypoint.sh`: ждёт доступности Postgres (ретраи подключения), `alembic upgrade head`, затем `exec uvicorn app.main:app --host 0.0.0.0 --port 8000`.
- `docker-compose.yml`: `db` (postgres:16-alpine, healthcheck `pg_isready`, порт наружу `5433:5432`) + `data-service` (`depends_on: db: condition: service_healthy`, порт `8001:8000`).
- `.env.example`: `DATABASE_URL`, `POSTGRES_USER/PASSWORD/DB`, `SESSION_ENCRYPTION_KEY`, `DEFAULT_LOCK_TTL_SECONDS`, `LOG_LEVEL`.

## Тесты

`testcontainers[postgres]` поднимает эфемерный Postgres для тестов, `alembic upgrade head` прогоняется программно в фикстуре, таблицы очищаются `TRUNCATE ... CASCADE` между тестами.

- **test_leads_bulk_dedup.py** — дубликат внутри одного batch и повторная отправка ранее вставленных лидов → `inserted/skipped` считаются верно, в БД нет дублей.
- **test_accounts_next_available_concurrency.py** (ключевой) — 3 аккаунта, 10 параллельных вызовов `next-available` через `ThreadPoolExecutor` → ровно 3 успеха с уникальными id, 7 раз 409.
- **test_accounts_lock_release_cooldown.py** — цикл next-available→release→снова доступен; cooldown с истечением по времени; permanent cooldown = banned навсегда.
- **test_messages_and_rate_limit.py** — аккаунт с `hourly_limit=2`, после 2 сообщений `next-available` даёт 409, после "истечения часа" — снова доступен.
- **test_templates_campaigns_crud.py** — smoke CRUD campaign→template→PATCH template_id.

---

## Context (второй проход — уточнение открытых вопросов и файл-за-файлом план)

БД-сервис (Data Service) — единственный, кто говорит с Postgres напрямую, остальные (Парсер, Отправка, Оркестратор) работают через его REST API. Выше уже есть подробная спецификация этого сервиса (схема из 6 таблиц, атомарная выдача аккаунтов через `FOR UPDATE SKIP LOCKED`, шифрование сессий Fernet, список эндпоинтов, тестовый план). Каркас папок под `E:\plan\data-service` уже создан, но пуст — ни одного `.py`-файла, ни alembic-конфига, ни Docker-артефактов, ни git-репозитория. Цель этой части — довести сервис от пустого каркаса до рабочего, задокументированного, протестированного и докеризованного REST API, строго следуя уже принятым выше решениям, ничего не переизобретая.

Текущее состояние (проверено):
- `E:\plan\data-service\` содержит пустые директории `app/{crud,models,routers,schemas}`, `alembic/versions`, `scripts`, `tests`; заполнены только `requirements.txt`, `.gitignore`, `.dockerignore`.
- Docker 29.6.2 + Compose v5.3.1 установлены. Git установлен, но репозиторий нигде под `E:\plan` не инициализирован.
- Глобальный Python — 3.14.6; сервис таргетит 3.12 (образ `python:3.12-slim`).

### Решения по открытым вопросам

- `Platform`: `vk` / `tg` / `instagram` (короткое `tg`, как в коде архитектуры встречается сокращение).
- `DeliveryStatus`: `sent` / `failed` / `pending`.
- `ReplyStatus`: `none` / `replied` / `no_reply`.
- Rate-limit окна (`hourly_limit`/`daily_limit`) — скользящие (`sent_at >= now() - interval '1 hour'/'1 day'`), не календарные.
- `ondelete` для FK: `sessions.account_id`, `messages.lead_id`, `messages.account_id` → `CASCADE` (история/сессия теряют смысл без родителя); `leads.campaign_id`, `campaigns.template_id`, `templates.campaign_id` → `SET NULL` (необязательные связи).
- Локальная разработка: поставить Python 3.12 локально (`winget install Python.Python.3.12` или официальный установщик), создать `.venv`, работать с моделями/alembic/pytest локально в быстром цикле; Docker (testcontainers) всё равно обязателен для concurrency-теста и финальной верификации через `docker compose up`.
- Ошибки: стандартный FastAPI `HTTPException(status_code, detail=...)` — `409` для `NoAccountAvailableError`, `404` для `NotFoundError`, без отдельного envelope.
- `AccountCreate` принимает только `platform, login, purpose, proxy?, user_agent?, viewport?, hourly_limit, daily_limit`; `status`, `warmup_stage`, `locked_until` и т.п. — серверные дефолты (`status=active`, `warmup_stage=0`). `viewport` — простая строка (`"1920x1080"`). Мутация статуса — только через `/lock /release /cooldown`, отдельного generic `PATCH /accounts/{id}` нет (вне списка эндпоинтов выше).
- Git-репозиторий инициализируется на уровне `E:\plan` (не только `data-service`), т.к. в будущем туда лягут соседние сервисы.

### Порядок реализации (файл за файлом)

Каждый шаг — файлы под `E:\plan\data-service\`, если не указано иное. При наполнении каждого пакета добавлять пустой `__init__.py` (`app/`, `app/models/`, `app/schemas/`, `app/crud/`, `app/routers/`, `tests/`).

**0. Git + окружение**
- `git init` в `E:\plan`, первый коммит после того как появится реальный код (не раньше шага 2).
- Установить Python 3.12 локально, создать `.venv` внутри `data-service`, `pip install -r requirements.txt`.

**1. `app/enums.py`** — `StrEnum`: `Platform(vk, tg, instagram)`, `LeadStatus(new, queued, contacted, replied, rejected)`, `CampaignStatus(draft, running, paused, completed)`, `AccountPurpose(parsing, messaging, both)`, `AccountStatus(active, flood, banned, cooldown, locked)`, `DeliveryStatus(sent, failed, pending)`, `ReplyStatus(none, replied, no_reply)`.

**2. `app/config.py`** — `Settings(BaseSettings)`: `DATABASE_URL`, `SESSION_ENCRYPTION_KEY`, `DEFAULT_LOCK_TTL_SECONDS=900`, `LOG_LEVEL="INFO"`, `model_config = SettingsConfigDict(env_file=".env")`; модульный `settings = Settings()`.

**3. `app/database.py`** — `engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)`; `SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)`; генератор `get_db()`.

**4. `app/models/`** — `base.py` (`Base(DeclarativeBase)`, `UUIDPK` mixin с `id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)`), затем `lead.py, campaign.py, account.py, session.py, message.py, template.py` — по одному классу на таблицу, поля и constraints как в схеме выше, плюс `Account.locked_until/locked_task_ref/last_used_at`. FK `campaigns.template_id` и `templates.campaign_id` объявляются в модели **без** `ForeignKey` на уровне колонки (просто `Mapped[UUID | None]`) — сам constraint добавляется в миграции. `app/models/__init__.py` импортирует и реэкспортирует все 6 классов, чтобы `Base.metadata` их видел.

**5. Alembic** — `alembic.ini` (`script_location = alembic`), `alembic/env.py` (берёт `DATABASE_URL` из `app.config.settings`, `target_metadata = Base.metadata`, sync-движок), `alembic/versions/0001_initial_schema.py`: сгенерировать через `alembic revision --autogenerate` против локального Postgres (см. шаг 9 — поднять `db` из compose раньше для этого), в конце ревизии — два явных `op.create_foreign_key(...)` с именами (`fk_campaigns_template_id_templates`, `fk_templates_campaign_id_campaigns`, `ondelete="SET NULL"`), `downgrade()` дропает их первыми.

**6. `app/security.py`** — обёртка над `Fernet(settings.SESSION_ENCRYPTION_KEY)`: `encrypt_json(obj: dict) -> bytes`, `decrypt_json(data: bytes) -> dict`.

**7. `app/exceptions.py`** — `NotFoundError`, `NoAccountAvailableError` (простые исключения, без деталей HTTP — маппинг в `main.py`).

**8. `app/schemas/`** — по файлу на сущность, Pydantic v2, `*Create/*Update/*Out` (`ConfigDict(from_attributes=True)` на `*Out`):
- `lead.py`: `LeadCreate`, `LeadBulkCreate = list[LeadCreate]`, `LeadBulkResult{inserted, skipped, lead_ids}`, `LeadStatusUpdate{status}`, `LeadOut`.
- `account.py`: `AccountCreate` (поля см. раздел решений выше), `AccountOut` (+ вычисляемые `hourly_used`, `daily_used`, не из `from_attributes`), `NextAvailableRequest{platform, purpose, lock_ttl_seconds=900, task_ref: str|None}`, `CooldownRequest{minutes, permanent=False, reason: str|None}`.
- `session.py`: вход/выход — «сырой» `storage_state: dict`, шифрование скрыто в CRUD.
- `message.py`, `campaign.py`, `template.py` — стандартные `Create/Out`.

**9. `crud/leads.py` + `routers/leads.py`**
- `bulk_insert(db, leads) -> LeadBulkResult`: `pg_insert(Lead).values(...).on_conflict_do_nothing(index_elements=["platform","external_id"]).returning(Lead.id)`.
- `list_leads(db, status=None, platform=None, campaign_id=None, limit=50, offset=0)`, `update_status(db, lead_id, status)` (404 через `NotFoundError`).
- Роутер: `POST /leads/bulk`, `GET /leads`, `PATCH /leads/{id}/status`.

**10. `crud/accounts.py` + `routers/accounts.py`** (ключевая логика):
- `next_available(db, platform, purpose, lock_ttl_seconds, task_ref)`: `SELECT` кандидатов (`platform`, `purpose IN (purpose, both)`, `status=active OR (status=locked AND locked_until<now())`, не в активном cooldown), `ORDER BY warmup_stage, last_used_at NULLS FIRST`, `LIMIT 20`, `.with_for_update(skip_locked=True)`; пусто → `NoAccountAvailableError`; иначе для каждого кандидата проверить скользящий rate-limit по `messages` (первый прошедший — `status=locked`, `locked_until=now()+ttl`, `locked_task_ref`, `last_used_at=now()`, commit, return); если после фильтра никого — `NoAccountAvailableError`.
- `lock/release/cooldown(db, id, ...)` — точечный `SELECT ... FOR UPDATE` по id + мутация + commit; `cooldown(permanent=True)` → `status=banned`.
- `list_accounts(db, status=None, platform=None, purpose=None)` — с join/подзапросом по `messages` для `hourly_used/daily_used`.
- Роутер: `POST /accounts`, `GET /accounts`, `POST /accounts/next-available` (409 при `NoAccountAvailableError`), `POST /accounts/{id}/lock|/release|/cooldown`, `PUT/GET /accounts/{id}/session` (вызывает `crud/sessions.py`).

**11. `crud/sessions.py`, `crud/messages.py`, `crud/campaigns.py`, `crud/templates.py` + соответствующие роутеры** — `sessions.py` использует `app/security.py` для upsert/get; `messages.py` содержит helper скользящего rate-limit, используемый `accounts.py` (чтобы избежать циклического импорта — `accounts.py` импортирует функцию из `messages.py`, не наоборот); остальные — обычный CRUD.

**12. `app/main.py`** — `FastAPI(title="Data Service")`, регистрация всех 6 роутеров, `GET /health`, `add_exception_handler` для `NotFoundError`→404 и `NoAccountAvailableError`→409.

**13. Docker**
- `Dockerfile`: `python:3.12-slim`, `pip install -r requirements.txt`, `COPY . .`, entrypoint через `scripts/entrypoint.sh`.
- `scripts/entrypoint.sh`: retry-ожидание доступности Postgres → `alembic upgrade head` → `exec uvicorn app.main:app --host 0.0.0.0 --port 8000`.
- `docker-compose.yml`: `db` (`postgres:16-alpine`, healthcheck `pg_isready`, `5433:5432`, volume) + `data-service` (`build: .`, `depends_on: db: condition: service_healthy`, `env_file: .env`, `8001:8000`).
- `.env.example`: `DATABASE_URL`, `POSTGRES_USER/PASSWORD/DB`, `SESSION_ENCRYPTION_KEY` (с пометкой — генерируется `Fernet.generate_key()`), `DEFAULT_LOCK_TTL_SECONDS`, `LOG_LEVEL`.

**14. Тесты** (`tests/conftest.py` — `PostgresContainer` из `testcontainers`, программный `alembic upgrade head`, `TRUNCATE ... CASCADE` между тестами, `TestClient` с override `get_db`):
1. `test_leads_bulk_dedup.py` — дубли внутри батча + повторная отправка уже вставленных → верные `inserted/skipped`, нет дублей в БД.
2. `test_accounts_next_available_concurrency.py` (ключевой) — 3 аккаунта, 10 параллельных вызовов через `ThreadPoolExecutor` (каждый поток бьёт по `TestClient`/HTTP, не шарит ORM-сессию) → ровно 3 успеха с уникальными id, 7×409.
3. `test_accounts_lock_release_cooldown.py` — next-available→release→снова доступен; cooldown с истёкшим временем; `permanent=True` → `banned`.
4. `test_messages_and_rate_limit.py` — `hourly_limit=2`, после 2 сообщений 409, после «истечения часа» (бэкдейченный `sent_at` через фикстуру напрямую в БД) — снова доступен.
5. `test_templates_campaigns_crud.py` — smoke CRUD campaign→template→PATCH template_id.

**15. `README.md`** — быстрый старт (`docker compose up --build`, генерация Fernet-ключа, `.env`), примеры `curl` по группам эндпоинтов, `pytest` (с пометкой про обязательный Docker), краткая ссылка на этот документ.

## Порядок реализации (укрупнённо, шаг 1 выполнен)

1. Скелет папок, `requirements.txt`, `.gitignore`/`.dockerignore`. ✅ выполнено
2. `enums.py`, `models/*` (все 6 таблиц), `database.py`, `config.py`.
3. Alembic init + ревизия `0001_initial_schema`.
4. `schemas/*`.
5. `crud/leads.py` + роутер (bulk insert с `ON CONFLICT`).
6. `crud/accounts.py` (`next_available`, `lock`, `release`, `cooldown`) + роутер.
7. `crud/messages.py`, `crud/sessions.py` (+ Fernet), `crud/campaigns.py`, `crud/templates.py` + роутеры.
8. `main.py`: сборка приложения, `/health`, exception handlers.
9. `Dockerfile`, `entrypoint.sh`, `docker-compose.yml`, `.env.example`.
10. Тесты: сначала dedup и concurrency, затем остальные.
11. `README.md`: запуск, примеры `curl`.

## Verification

1. `docker compose up --build` из `E:\plan\data-service` с нуля — сервис стартует, миграции накатываются, `GET http://localhost:8001/health` → `{"status":"ok"}`.
2. `POST /leads/bulk` с батчем, включающим дубликат — `inserted/skipped` верны; повторная отправка не создаёт дублей в БД (проверить `SELECT count(*)`).
3. Два подряд `POST /accounts/next-available` не возвращают один и тот же аккаунт; после `POST /accounts/{id}/release` аккаунт снова доступен.
4. `pytest` из `data-service/` (Docker обязателен для testcontainers) — все 5 тестовых файлов проходят, включая concurrency-тест (ровно 3 успеха / 7×409).
5. `alembic downgrade base` затем `alembic upgrade head` на чистой тестовой БД проходит без ошибок (проверяет корректность порядка FK в `downgrade()`).

## Следующие шаги

После зелёной верификации выше — переход к Фазе 1/2 общего плана: [`plan-parser-service.md`](plan-parser-service.md).
