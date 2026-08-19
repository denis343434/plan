# Data Service

Единственный сервис, говорящий с PostgreSQL напрямую. Хранит `leads`, `campaigns`,
`accounts`, `sessions`, `messages`, `templates`; наружу отдаёт REST API. Остальные
сервисы (Parser, Messaging, Orchestrator) обращаются сюда по HTTP, а не в базу.

Подробности решений и схема — [`../plan-data-service.md`](../plan-data-service.md),
общая архитектура — [`../architecture.md`](../architecture.md).

## Быстрый старт

```bash
cp .env.example .env
# сгенерировать ключ шифрования сессий и вписать в .env (SESSION_ENCRYPTION_KEY):
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

docker compose up --build
```

Миграции накатываются автоматически при старте контейнера (`scripts/entrypoint.sh`).

```bash
curl http://localhost:8001/health
# {"status":"ok"}
```

Swagger UI: http://localhost:8001/docs

## Примеры curl

### Leads

```bash
curl -X POST http://localhost:8001/leads/bulk -H "Content-Type: application/json" -d '[
  {"platform":"vk","external_id":"1","group_url":"https://vk.com/g1"},
  {"platform":"vk","external_id":"1","group_url":"https://vk.com/g1"}
]'
# {"inserted":1,"skipped":1,"lead_ids":["..."]}

curl "http://localhost:8001/leads?status=new&platform=vk"

curl -X PATCH http://localhost:8001/leads/{id}/status -H "Content-Type: application/json" \
  -d '{"status":"contacted"}'
```

### Accounts

```bash
curl -X POST http://localhost:8001/accounts -H "Content-Type: application/json" -d '{
  "platform":"vk","login":"acc1","purpose":"messaging","hourly_limit":10,"daily_limit":50
}'

curl "http://localhost:8001/accounts?status=active&platform=vk"

curl -X POST http://localhost:8001/accounts/next-available -H "Content-Type: application/json" \
  -d '{"platform":"vk","purpose":"messaging","lock_ttl_seconds":900}'
# 200 AccountOut или 409, если ничего не свободно

curl -X POST http://localhost:8001/accounts/{id}/release
curl -X POST http://localhost:8001/accounts/{id}/cooldown -H "Content-Type: application/json" \
  -d '{"minutes":30,"reason":"captcha"}'

curl -X PUT http://localhost:8001/accounts/{id}/session -H "Content-Type: application/json" \
  -d '{"storage_state":{"cookies":[]}}'
curl http://localhost:8001/accounts/{id}/session
```

### Messages / Campaigns / Templates

```bash
curl -X POST http://localhost:8001/messages -H "Content-Type: application/json" -d '{
  "lead_id":"...","account_id":"...","text_sent":"Привет!","delivery_status":"sent"
}'
curl "http://localhost:8001/messages?account_id=..."

curl -X POST http://localhost:8001/campaigns -H "Content-Type: application/json" -d '{
  "name":"Summer promo","platform":"vk","keyword":"fitness"
}'
curl -X POST http://localhost:8001/templates -H "Content-Type: application/json" -d '{
  "campaign_id":"...","variant":"A","body":"Hi {{org_name}}!"
}'
```

## Тесты

```bash
python -m venv .venv && .venv\Scripts\activate   # Windows
pip install -r requirements.txt
pytest
```

Тесты используют `testcontainers[postgres]` — **Docker обязателен** и должен быть запущен,
даже если сами тесты гоняются локально вне контейнера. `alembic upgrade head` применяется
программно в фикстуре, таблицы очищаются `TRUNCATE ... CASCADE` между тестами.

Ключевой тест — `test_accounts_next_available_concurrency.py`: 3 аккаунта, 10 параллельных
`POST /accounts/next-available` через `ThreadPoolExecutor` → ровно 3×200 с уникальными id, 7×409.
Это подтверждает, что `SELECT ... FOR UPDATE SKIP LOCKED` действительно исключает гонку.

## Локальная разработка без Docker (быстрый цикл)

Python 3.12 обязателен (образ — `python:3.12-slim`).

```bash
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
docker compose up -d db            # только Postgres, порт 5433
alembic upgrade head
uvicorn app.main:app --reload --port 8001
```

`pytest` всё равно поднимает свой Postgres через testcontainers — Docker должен быть запущен.
