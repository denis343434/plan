# Orchestrator Service

Единственная точка входа для пользователя. Своего хранилища кампаний не заводит — кампании
уже живут в [Data Service](../data-service) (`CRUD /campaigns`). Координирует
[Parser Service](../parser-service) и [Messaging Service](../messaging-service) через REST +
polling, без брокера очередей.

Подробности решений — [`../plan-orchestrator-service.md`](../plan-orchestrator-service.md),
общая архитектура — [`../architecture.md`](../architecture.md).

## Быстрый старт

Data Service, Parser Service и Messaging Service должны быть уже подняты.

```bash
cp .env.example .env

docker build -t orchestrator-service .
docker run --rm -p 8004:8000 --env-file .env \
  -e DATA_SERVICE_URL=http://host.docker.internal:8001 \
  -e PARSER_SERVICE_URL=http://host.docker.internal:8002 \
  -e MESSAGING_SERVICE_URL=http://host.docker.internal:8003 \
  orchestrator-service
```

> Отдельного `docker-compose.yml` у Orchestrator Service нет — по решению из `architecture.md`
> ("Compose-топология"), один compose только у Data Service для его изолированной разработки;
> остальные три сервиса собираются/запускаются как обычные Docker-образы до появления
> корневого `docker-compose.yml` (Фаза 5).

```bash
curl http://localhost:8004/health
# {"status":"ok"}
```

## Пример запроса

```bash
curl -X POST http://localhost:8004/campaigns -H "Content-Type: application/json" -d '{
  "name": "fitness-outreach", "platform": "vk", "keyword": "fitness"
}'
# 201 {"id":"...","name":"fitness-outreach","platform":"vk","keyword":"fitness","template_id":null,"status":"draft","created_at":"..."}

curl -X POST http://localhost:8004/campaigns/{campaign_id}/start
# 202 {"campaign_id":"...","phase":"idle"}

curl http://localhost:8004/campaigns/{campaign_id}
# {"campaign":{...,"status":"running"},"phase":"parsing","error":null,
#  "stats":{"new":0,"queued":0,"contacted":0,"replied":0,"rejected":0}}

# ... поллинг ...
curl http://localhost:8004/campaigns/{campaign_id}
# {"campaign":{...,"status":"completed"},"phase":"done","error":null,
#  "stats":{"new":0,"queued":0,"contacted":3,"replied":0,"rejected":0}}
```

Повторный `POST .../start`, пока фаза `parsing`/`messaging`, не запускает вторую параллельную
задачу — возвращает текущую фазу.

## Логика `run_campaign_flow`

1. `GET /campaigns/{id}` в Data Service, статус кампании → `running`.
2. `POST /parse` в Parser Service, поллинг `GET /parse/{task_id}/status` каждые
   `POLL_INTERVAL_SEC` до `done`/`failed`/таймаута `POLL_TIMEOUT_SEC`.
3. Если парсинг `done` → `POST /campaigns/{id}/send` в Messaging Service, аналогичный поллинг
   `GET /campaigns/{id}/send-status` (`waiting_for_account` тоже считается терминальной
   неудачей для поллинга — сам по себе он не рассосётся без внешнего вмешательства).
4. Оба этапа `done` → статус кампании в Data Service → `completed`, in-memory фаза → `done`.
5. Ошибка/таймаут на любом этапе → статус кампании в Data Service → `paused` (единственный
   подходящий терминал из `draft/running/paused/completed` — там нет `failed`), реальная
   причина сохраняется только в in-memory `OrchestrationTask.error` и отдаётся через
   `GET /campaigns/{id}`.

`GET /campaigns/{id}` сливает состояние `OrchestrationTask` из памяти (или `phase=idle`, если
`/start` не вызывался) со статистикой из Data Service: для каждого `status` в
`new/queued/contacted/replied/rejected` — `len(GET /leads?campaign_id=&status=X&limit=1000)`.
Приближение — в API Data Service нет `COUNT`-эндпоинта, приемлемо при MVP-объёмах.

`POST /accounts/{id}/pause` — тонкий прокси к `POST /accounts/{id}/cooldown` в Data Service
(по умолчанию `minutes=10080` — 7 дней; `permanent=true` переводит аккаунт в `banned`).

## Тесты

```bash
python -m venv .venv && .venv\Scripts\activate   # Windows
pip install -r requirements.txt
pytest
```

Все тесты работают на моках (`respx` мокает HTTP-вызовы ко всем трём downstream-сервисам) и не
требуют ни одного из них живым:

- `test_orchestration_state_machine.py` — `run_campaign_flow`: `parsing→messaging→done`,
  провал на этапе парсинга, `waiting_for_account` на этапе рассылки, таймаут поллинга,
  недоступность Parser Service — во всех неуспешных случаях кампания уходит в `paused`.
- `test_campaigns_endpoint_mocked.py` — `POST /campaigns` как прозрачный прокси,
  `GET /campaigns/{id}` (агрегация stats, 404), `POST /campaigns/{id}/start`
  (полный прогон и защита от повторного запуска).
- `test_accounts_pause.py` — `POST /accounts/{id}/pause` с дефолтным cooldown и с `permanent`.
