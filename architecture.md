# Архитектура проекта (4 микросервиса)

По рисунку — не 6 сервисов, а **4**, БД — отдельный сервис (не просто общая база), Оркестратор — центр, к которому идут все стрелки, а Парсер и Отправка общаются с БД напрямую, минуя Оркестратор.

```
                 ┌─────────────────────────────────────┐
                 │                                       │
      ┌──────────▼─────────┐              ┌─────────────┴────────┐
      │  Отправка сообщений │◄────────────►│      Оркестратор      │
      │     (Messaging)      │              │  (API Gateway + Core) │
      └──────────┬──────────┘              └─────────────┬────────┘
                 │                                        │
                 │            ┌──────────────┐            │
                 └───────────►│      БД      │◄───────────┘
                               │ (Data Service)│
                 ┌───────────►│  leads/accounts/
                 │             │ sessions/msgs │
      ┌──────────┴──────────┐ └──────┬───────┘
      │       Парсер         │◄──────┘
      │     (Parser)         │
      └───────────┬──────────┘
                   │
                   └────────────► Оркестратор (запуск задачи парсинга)
```

Итого 4 микросервиса: **Парсер**, **БД (Data Service)**, **Отправка сообщений (Messaging)**, **Оркестратор**. Отдельных Leads Service, Account Manager и Monitoring — нет, их функции поглощены сервисом БД (хранение) и Оркестратором (управление/метрики).

Этот документ — главный: общая архитектура, контракты между сервисами, общий стек, топология репозитория и конвенции. Пошаговые планы реализации каждого сервиса — в отдельных файлах:

- [`plan-data-service.md`](plan-data-service.md) — БД (Data Service)
- [`plan-parser-service.md`](plan-parser-service.md) — Парсер (Parser Service)
- [`plan-messaging-service.md`](plan-messaging-service.md) — Отправка сообщений (Messaging Service)
- [`plan-orchestrator-service.md`](plan-orchestrator-service.md) — Оркестратор (Orchestrator Service)

---

## Обзор сервисов

### 1. Парсер (Parser Service)

**Зона ответственности:** только сбор данных, без бизнес-логики рассылки.

- Сейчас: 1 адаптер — **VK**, ищет группы по тематике/ключу без сайта (`has_site == false` в фильтре).
- VK-адаптер реализован **через Playwright** (эмуляция браузера), не через официальное API — значит парсеру, как и рассылке, нужен залогиненный аккаунт (см. ниже про пул аккаунтов) и та же анти-бан дисциплина: задержки, прокси, ограничение частоты запросов на аккаунт. Логика поиска групп — навигация по поиску VK и разбор DOM, а не вызовы API-методов.
- TG/Instagram — интерфейс адаптера уже общий, реализации — заглушки (`NotImplementedAdapter`, возвращает пустой список + лог "not supported yet"), чтобы Оркестратор мог дергать любой `platform` не падая.

**Общий интерфейс адаптера:**
```python
class SourceAdapter(Protocol):
    def search_communities(self, keyword: str, filters: ParseFilters) -> list[RawLead]: ...
```

**API:**
- `POST /parse` — `{platform: "vk", keyword: "развлечения", filters: {...}, campaign_id}` → запускает парсинг (асинхронно, задача кладётся в очередь или выполняется в фоне), пишет найденные лиды напрямую в **БД-сервис** (`POST /leads/bulk`).
- `GET /parse/{task_id}/status`

**Вызывается:** только Оркестратором (запуск задачи). Пишет: только в БД.

Детальный план реализации → [`plan-parser-service.md`](plan-parser-service.md).

---

### 2. БД (Data Service)

Единая точка хранения — заменяет Leads Service + Account Manager из предыдущей версии. Внутри — PostgreSQL, наружу — REST API (никто не лезет в Postgres напрямую, кроме этого сервиса — так проще держать дедупликацию и лимиты в одном месте).

**Таблицы:**

| Таблица | Ключевые поля |
|---|---|
| `leads` | `id`, `platform`, `external_id` (уник. по `platform+external_id` — тут и есть идемпотентность), `group_url`, `admin_contact`, `title`, `status` (`new/queued/contacted/replied/rejected`), `campaign_id`, `found_at` |
| `campaigns` | `id`, `name`, `platform`, `keyword`, `template_id`, `status`, `created_at` |
| `accounts` | `id`, `platform`, `login`, `purpose` (`parsing/messaging/both`), `proxy`, `user_agent`, `viewport`, `hourly_limit`, `daily_limit`, `status` (`active/flood/banned/cooldown`), `warmup_stage`, `cooldown_until` |
| `sessions` | `account_id`, `storage_state` (JSON — Playwright `context.storage_state()`, куки/localStorage, зашифровано), `updated_at` |
| `messages` | `id`, `lead_id`, `account_id`, `template_variant`, `text_sent`, `sent_at`, `delivery_status`, `reply_status` |
| `templates` | `id`, `campaign_id`, `variant` (A/B), `body` (с плейсхолдерами `{{org_name}}` и т.п.) |

**API (основные эндпоинты):**
- `POST /leads/bulk` — вставка с `ON CONFLICT (platform, external_id) DO NOTHING` → защита от дублей "из коробки" на уровне БД, а не приложения.
- `GET /leads?status=new&platform=vk&campaign_id=...` — выборка для рассылки.
- `PATCH /leads/{id}/status` — обновление статуса (`contacted`, `replied`...).
- `GET /accounts?status=active&platform=vk` — список аккаунтов с текущей нагрузкой (лимиты в час/сутки считаются здесь же по `messages`).
- `POST /accounts/next-available` — **атомарная** выдача одного свободного аккаунта под задачу (`purpose`, `platform`), реализуется через `SELECT ... FOR UPDATE SKIP LOCKED`, сразу переводит аккаунт в `locked`/занят на время задачи. Это устраняет гонку, когда два воркера (или Парсер и Отправка) одновременно хотят один и тот же аккаунт.
- `POST /accounts/{id}/lock` / `/release` — ручной lock/release (аварийная пауза, обслуживание сессии).
- `POST /accounts/{id}/cooldown` — перевести аккаунт в `flood`/`cooldown` на N минут (вызывается адаптером при обнаружении капчи/ограничения VK).
- `POST /messages` — лог отправки.
- CRUD `/templates`, `/campaigns`.

**Кто пишет/читает:** Парсер (лиды), Отправка (лиды, аккаунты, сообщения), Оркестратор (кампании, статусы, метрики).

Детальный план реализации (схема, миграции, тесты, docker) → [`plan-data-service.md`](plan-data-service.md).

---

### 3. Отправка сообщений (Messaging Service)

**Зона ответственности:** только отправка, ничего не знает о том, откуда взялись лиды.

- Внутри — выбор канала (`platform`): пока реально работает только **VK**, через Playwright (эмуляция реального браузера с залогиненным аккаунтом), TG (Telethon) и Instagram — заглушки, которые просто логируют "would send" и помечают лид как `contacted` в dry-run режиме, чтобы пайплайн можно было гонять целиком уже сейчас.
- Берёт лиды из БД (`status=new`), запрашивает свободный аккаунт через `/accounts/next-available` (атомарно, без гонки), рендерит шаблон (A/B по `template_variant`, простая подстановка `{{org_name}}`), отправляет с рандомной задержкой (`random.uniform(min_delay, max_delay)` из конфига) и с учётом `hourly_limit/daily_limit`.
- После отправки — `PATCH /leads/{id}/status = contacted` и `POST /messages` в БД. Это и есть anti-duplicate: если лид уже `contacted`, повторный проход его не берёт.
- Подробности распределения нагрузки и антибан-механики между VK-аккаунтами — см. раздел ["VK через Playwright"](#vk-через-playwright-распределение-нагрузки-и-антибан) ниже.

**API:**
- `POST /campaigns/{id}/send` — запустить рассылку по кампании (асинхронно, воркер идёт по очереди из БД).
- `GET /campaigns/{id}/send-status`

**Вызывается:** Оркестратором. Общается: только с БД (за лидами/аккаунтами/шаблонами и для записи результата).

Детальный план реализации → [`plan-messaging-service.md`](plan-messaging-service.md).

---

### 4. Оркестратор (Orchestrator / API Gateway)

Единственная точка входа для пользователя (тебя). Собирает всё в кампанию.

**Обязанности (по рисунку — "курировать всем этим"):**
1. Единый REST-вход: `POST /campaigns` (создать), `POST /campaigns/{id}/start` (последовательно вызывает Парсер → ждёт/опрашивает → вызывает Отправку).
2. Распределение нагрузки по аккаунтам — логика "какой аккаунт свободен и не перегрет" физически лежит в БД (`/accounts`), но *решение* о том, сколько кампаний параллельно гонять и когда переключать аккаунт при флуд-контроле — здесь.
3. Вызывает остальные сервисы (Парсер, Отправка) по REST/через очередь задач.
4. Отдаёт статус кампании и лёгкие метрики (`GET /campaigns/{id}/stats` — агрегирует данные из БД: сколько найдено/отправлено/забанено/ответили), т.е. функцию Monitoring он берёт на себя как read-model поверх БД — отдельного сервиса метрик нет.

**API:**
- `POST /campaigns` — `{platform, keyword, template_id}`
- `POST /campaigns/{id}/start`
- `GET /campaigns/{id}` — статус + агрегированная статистика
- `POST /accounts/{id}/pause` — ручной аварийный стоп аккаунта

Детальный план реализации → [`plan-orchestrator-service.md`](plan-orchestrator-service.md).

---

## VK через Playwright: распределение нагрузки и антибан

VK-адаптер (и в Парсере, и в Отправке) — это управляемый Playwright-браузер, залогиненный под конкретным аккаунтом, а не вызов API. Отсюда набор требований, которых не было бы при работе через официальный API:

**Пул аккаунтов**
- В `accounts` у каждой VK-записи — `proxy`, `user_agent`, `viewport`, привязанные к ней раз и навсегда (не менять между запусками — смена fingerprint у одного и того же аккаунта — сигнал для антифрода VK).
- В `sessions` хранится `storage_state` — экспорт куки/localStorage из Playwright-контекста, чтобы не логиниться заново каждый прогон (повторный логин чаще триггерит проверки, чем переиспользование сессии).
- Поле `purpose` разделяет аккаунты для парсинга и для рассылки (`parsing/messaging/both`) — так рассылочный лимит аккаунта не сжирается просмотром групп парсером. По умолчанию — разные пулы.

**Распределение нагрузки**
- Атомарная выдача — через `/accounts/next-available` в БД-сервисе (`SELECT ... FOR UPDATE SKIP LOCKED`), стратегию выбора (round-robin / least-loaded / с учётом `warmup_stage`) настраивает Оркестратор как политику, а не выбирает аккаунт вручную на каждое сообщение.
- Один аккаунт = один Playwright browser context = один воркер. Разные аккаунты можно гонять параллельно (несколько контекстов/процессов), но **внутри одного аккаунта действия строго последовательны** — распараллеливание одного и того же аккаунта — прямой путь к бану.
- Оркестратор задаёт, сколько аккаунтов одновременно участвует в кампании (пул из N воркеров), БД гарантирует, что каждый лид/сообщение обрабатывается ровно одним аккаунтом в моменте.

**Rate limit и «прогрев»**
- `hourly_limit`/`daily_limit` — жёсткий потолок на аккаунт, считается по факту записей в `messages` за период; при достижении — аккаунт временно исключается из выдачи (`next-available` его не отдаёт) без перевода в `flood`/`banned`.
- `warmup_stage` — новый аккаунт стартует с заниженными лимитами (например 5–10 сообщений/сутки) и наращивает их по расписанию (день 1 → день 7 → полный лимит), а не сразу работает на полную.
- Между сообщениями — случайная задержка (`min_delay_sec`/`max_delay_sec` из конфига), плюс имитация поведения на странице (скролл, пауза перед вводом текста, посимвольный ввод с разбросом) — это уже забота самого VK-адаптера внутри Playwright, а не Оркестратора.

**Обнаружение бана/флуд-контроля**
- Адаптер после каждого действия проверяет DOM на признаки капчи/ограничения ("слишком много сообщений", форма капчи, редирект на проверку) — при обнаружении сразу зовёт `POST /accounts/{id}/cooldown` (или переводит в `banned`, если это финальная блокировка), текущая задача не ретраится на этом же аккаунте.
- Оркестратор при старте кампании выбирает только аккаунты `status=active`, `cooldown`/`flood`/`banned` не участвуют, пока не истечёт `cooldown_until` или их не разбанят вручную.
- Открытый вопрос на реализацию: капчу автоматически не решаем — при её появлении аккаунт просто ставится на паузу и это видно в статусе кампании (нужен ручной разбор либо интеграция антикапчи, если объём вырастет).

---

## Взаимодействие (соответствует стрелкам на рисунке)

| Связь | Тип | Что происходит |
|---|---|---|
| Оркестратор → Парсер | REST (async task) | запуск парсинга по кампании |
| Оркестратор → Отправка | REST (async task) | запуск рассылки по кампании |
| Оркестратор ↔ БД | REST | чтение статусов/метрик, управление аккаунтами |
| Парсер → БД | REST | запись найденных лидов (bulk, дедуп на уровне уник. индекса) |
| Отправка ↔ БД | REST | чтение лидов/аккаунтов/шаблонов, запись сообщений и статусов |

Очередь (RabbitMQ/Redis Streams) — опционально между Оркестратором и Парсером/Отправкой, если задачи должны переживать рестарт сервиса и не блокировать REST-вызов (парсинг и рассылка — долгие). Для MVP можно и без брокера: Оркестратор дергает REST и опрашивает статус (`polling`), брокер добавляется, когда объём вырастет.

---

## Стек и запуск

- Python 3.12, FastAPI — на каждый из 4 сервисов.
- PostgreSQL — только у сервиса БД (остальные к нему не подключаются напрямую).
- RabbitMQ/Redis — опционально, для очереди задач Оркестратор→Парсер/Отправка.
- **Playwright (Python)** — VK-адаптер в Парсере и в Отправке (браузерная эмуляция, отдельный контекст на аккаунт, прокси и fingerprint из `accounts`).
- Telethon — заготовлен в Messaging/Parser под TG-адаптер (сейчас не задействован).
- Docker Compose: 4 контейнера сервисов + postgres (+ rabbitmq/redis при необходимости).
- Конфиг: `.env` на каждый сервис — лимиты, задержки, ключи API/сессии, connection string к БД-сервису (не к Postgres — только сервис БД знает свою базу).

---

## Топология репозитория

```
E:\plan\
├── architecture.md
├── plan-data-service.md
├── plan-parser-service.md
├── plan-messaging-service.md
├── plan-orchestrator-service.md
├── .git\                        # инициализируется на шаге 0 Data Service, не повторно
├── data-service\                 # см. plan-data-service.md
├── parser-service\               # новое, см. plan-parser-service.md
├── messaging-service\            # новое, см. plan-messaging-service.md
├── orchestrator-service\         # новое, см. plan-orchestrator-service.md
└── docker-compose.yml            # новый, корневой — единственный compose для полной интеграции
```

Один корневой `docker-compose.yml`, а не по одному на сервис — раздел "Стек и запуск" выше прямо говорит "Docker Compose: 4 контейнера сервисов + postgres", то есть один общий стек. `data-service/docker-compose.yml` (`db + data-service`, порты `5433`/`8001`) остаётся как есть — используется для изолированной разработки/тестов самого Data Service до появления остальных трёх сервисов; для этого в `.env` разрабатываемого нового сервиса указывается `DATA_SERVICE_URL=http://localhost:8001`.

Корневой `docker-compose.yml` — сеть `backend`, сервисы обращаются друг к другу по имени контейнера (`http://data-service:8000`, не `localhost`):

| Сервис | Внутренний порт | Порт на хосте |
|---|---|---|
| postgres | 5432 | 5433 |
| data-service | 8000 | 8001 |
| parser-service | 8000 | 8002 |
| messaging-service | 8000 | 8003 |
| orchestrator-service | 8000 | 8004 (единая точка входа пользователя) |

---

## Общие конвенции для Парсера/Отправки/Оркестратора

Упрощённая версия структуры Data Service — без `models/alembic/database.py`, т.к. своей БД ни у одного из трёх нет (правило "PostgreSQL — только у сервиса БД" распространяется и сюда):

```
{service}-service\
├── app\
│   ├── main.py             # FastAPI() + роутеры + /health + exception handlers
│   ├── config.py            # Settings(BaseSettings): DATA_SERVICE_URL, LOG_LEVEL, ...
│   ├── logging_conf.py       # stdlib logging, единый форматтер (level, ts, service, msg)
│   ├── clients\
│   │   └── data_service.py   # DataServiceClient — httpx-обёртка, типизированные методы
│   ├── schemas\               # Pydantic v2, входные/выходные модели своего REST API
│   ├── routers\                # тонкие, вызывают tasks.py/orchestration.py
│   └── exceptions.py            # DataServiceError / DataServiceNotFoundError / NoAccountAvailableError
├── tests\
├── scripts\entrypoint.sh          # exec uvicorn app.main:app --host 0.0.0.0 --port 8000 (без alembic/DB-wait)
├── Dockerfile
├── requirements.txt
├── .env.example
├── .dockerignore / .gitignore
└── README.md
```

- **`app/clients/data_service.py`** — `httpx.Client(base_url=settings.DATA_SERVICE_URL, timeout=...)`, по одному методу на каждый реально используемый *этим* сервисом эндпоинт Data Service. Единая обработка ошибок: 404 → `DataServiceNotFoundError`, 409 → `NoAccountAvailableError`, остальные не-2xx/сетевые → `DataServiceError`. Ретраи: ручной цикл на 1 повтор с паузой 0.5с только для `ConnectError`/`TimeoutException` — без новых зависимостей вроде `tenacity`, объём запросов MVP этого не требует.
- **`/health`** — только liveness (`{"status": "ok"}`), без проверки доступности Data Service — иначе рестарт Data Service каскадно валит health остальных сервисов.
- **Dockerfile** — два паттерна: Orchestrator (как Data Service) — обычный `python:3.12-slim`, без браузера; Parser и Messaging — `python:3.12-slim` → `pip install -r requirements.txt` → `RUN playwright install --with-deps chromium` (ставит браузер и системные зависимости через apt внутри slim-образа).
- `requirements.txt` Parser/Messaging включают `playwright` и `telethon` (последний импортируется про запас, не используется, пока TG — заглушка).

---

## Интеграционная верификация (все 4 сервиса)

**Fake/dry-run VK режим обязателен для тестируемости без реальных VK-креды.** В `.env.example` Parser и Messaging: `VK_ADAPTER_MODE=fake` по умолчанию (dev/CI), `playwright` — для боевого запуска с реальным аккаунтом. В `fake`-режиме:

- Parser: `FakeVkAdapter` возвращает детерминированный набор `RawLead` без запуска браузера, но всё равно проходит через `next_available_account`/`get_session`/`release_account` — так тестируется реальная locking-логика Data Service, а не только сам факт парсинга.
- Messaging: тот же `DryRunAdapter`, что и для tg/instagram — тоже проходит через реальное получение аккаунта/лимиты/запись `messages`, просто не открывает браузер и не шлёт реальное сообщение.

**E2E smoke-флоу** (документируется в корневом `E:\plan\README.md`):

1. `docker compose up --build` из `E:\plan` (корень) — поднимает `postgres`, `data-service`, `parser-service`, `messaging-service`, `orchestrator-service`, Parser/Messaging в режиме `VK_ADAPTER_MODE=fake`.
2. `POST http://localhost:8004/campaigns` `{platform: "vk", keyword: "test", ...}`.
3. `POST http://localhost:8004/campaigns/{id}/start`.
4. Поллинг `GET http://localhost:8004/campaigns/{id}` до `status=completed`.
5. Контроль напрямую через Data Service: `GET http://localhost:8001/leads?campaign_id=...` — fake-лиды вставились и перешли в `contacted`.

---

## Итоговый порядок исполнения (все сервисы)

| Фаза | Что | Верификация | План |
|---|---|---|---|
| 0 | Data Service — довести до конца по его 15-шаговому плану (блокирует всё остальное) | `docker compose up --build` из `data-service/`, `pytest` зелёный (5 файлов, включая concurrency-тест) | [plan-data-service.md](plan-data-service.md) |
| 1 | Playwright-профиль аккаунта — `parser-service/scripts/vk_manual_login.py` (пишется вместе с Parser, операционно отдельная задача); не блокирует автоматическую верификацию благодаря `VK_ADAPTER_MODE=fake` | ручная проверка при наличии реального VK-аккаунта; в текущем окружении — пропускается | [plan-parser-service.md](plan-parser-service.md) |
| 2 | Parser Service | standalone против живого Data Service (Phase 0) на `FakeVkAdapter`; `pytest` | [plan-parser-service.md](plan-parser-service.md) |
| 3 | Messaging Service | standalone против живого Data Service; `pytest` | [plan-messaging-service.md](plan-messaging-service.md) |
| 4 | Orchestrator Service | против живых Parser+Messaging+Data Service; `pytest` на моках | [plan-orchestrator-service.md](plan-orchestrator-service.md) |
| 5 | Интеграция — корневой `docker-compose.yml` + e2e smoke | полный `docker compose up` из `E:\plan`, флоу campaign→start→leads→contacted→completed | (этот документ, раздел "Интеграционная верификация") |
| 6 (вне объёма) | Замена tg/instagram заглушек на реальные адаптеры | отложено явным пунктом | — |

---

## Общие решения по открытым вопросам (Parser/Messaging/Orchestrator)

| Вопрос | Решение | Почему |
|---|---|---|
| Compose-топология | один корневой `docker-compose.yml`, без отдельных compose у Parser/Messaging/Orchestrator | раздел "Стек и запуск" говорит об одном стеке "4 контейнера + postgres" |
| Хранение статуса задач (`/parse/{id}/status`, `/send-status`, orchestration) | in-memory dict + FastAPI `BackgroundTasks` во всех трёх сервисах | своей БД у них быть не должно; MVP явно разрешает polling без брокера |
| `/health` | только liveness, без проверки Data Service | избежать каскадного unhealthy при рестарте Data Service |
| Playwright API | синхронный (`sync_api`), не async | совместимо с `BackgroundTasks` (threadpool) и sync-выбором Data Service |
| Dockerfile Parser/Messaging | `playwright install --with-deps chromium` поверх `python:3.12-slim` | нужен реальный браузер в контейнере |
| Тестируемость VK без креды | `VK_ADAPTER_MODE=fake\|playwright` в Parser и Messaging | делает e2e прогоняемым без реального логина |
| Гранулярность выдачи аккаунта в Messaging | новый `next-available` на каждое сообщение, не один на всю кампанию | Data Service — единственный источник правды по лимитам; корректность важнее издержек на контекст |
| `send-status` в Messaging | из in-memory счётчиков задачи, не из Data Service | у `/messages` нет фильтра по `campaign_id` |
| Stats в Orchestrator | сумма `len(GET /leads?campaign_id=&status=X)` по каждому статусу | нет `COUNT`-эндпоинта в API Data Service; ок для MVP-объёмов |
| Терминальный статус кампании при сбое | `paused` (в Data Service) + детальная причина только в памяти Оркестратора | в `CampaignStatus` нет `failed` |
| `POST /accounts/{id}/pause` | прокси к существующему `POST /accounts/{id}/cooldown` | не создавать новый эндпоинт в Data Service |
| `vk_manual_login.py` | дублируется в parser-service и messaging-service, не выносится в общую библиотеку | осознанная мелкая дупликация вместо преждевременной абстракции на MVP-масштабе |
| Ретраи HTTP-клиента к Data Service | 1 повтор с 0.5с паузой только на сетевые ошибки, без новых зависимостей | минимализм для MVP-объёма запросов |

Специфичные для Data Service открытые вопросы (enum-значения, ondelete для FK и т.п.) — в [`plan-data-service.md`](plan-data-service.md).
