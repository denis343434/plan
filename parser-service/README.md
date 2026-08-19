# Parser Service

Собирает лиды (сообщества/группы) по ключевому слову и пишет их напрямую в
[Data Service](../data-service) (`POST /leads/bulk`). Не хранит своих данных —
своей БД нет, статус задач держится в памяти процесса.

Подробности решений — [`../plan-parser-service.md`](../plan-parser-service.md),
общая архитектура и антибан-логика VK — [`../architecture.md`](../architecture.md).

## Быстрый старт

Data Service должен быть уже поднят (см. `../data-service/README.md`).

```bash
cp .env.example .env
# .env по умолчанию: VK_ADAPTER_MODE=fake — не требует реального VK-аккаунта.

docker build -t parser-service .
docker run --rm -p 8002:8000 --env-file .env \
  -e DATA_SERVICE_URL=http://host.docker.internal:8001 \
  parser-service
```

> Отдельного `docker-compose.yml` у Parser Service нет — по решению из `architecture.md`
> ("Compose-топология") один сервис-компоуз только у Data Service для его изолированной
> разработки, остальные три сервиса собираются/запускаются как обычные Docker-образы до
> появления корневого `docker-compose.yml` (Фаза 5).

```bash
curl http://localhost:8002/health
# {"status":"ok"}
```

## Пример запроса

```bash
curl -X POST http://localhost:8002/parse -H "Content-Type: application/json" -d '{
  "platform": "vk",
  "keyword": "fitness"
}'
# 202 {"task_id":"...","status":"queued"}

curl http://localhost:8002/parse/{task_id}/status
# {"task_id":"...","status":"done","found":3,"inserted":3,"skipped":0,"error":null}
```

С `VK_ADAPTER_MODE=fake` (по умолчанию) задача всё равно проходит через реальные
`POST /accounts/next-available` → `GET /accounts/{id}/session` → `POST /accounts/{id}/release`
в Data Service — так тестируется настоящая locking-логика, а не только факт парсинга.
`FakeVkAdapter` просто не открывает браузер и возвращает детерминированный набор лидов.

## Реальный VK (Playwright)

1. Создать аккаунт в Data Service (`POST /accounts`, `purpose=parsing` или `both`).
2. Один раз залогиниться вручную и сохранить сессию:
   ```bash
   python scripts/vk_manual_login.py --account-id <uuid> --data-service-url http://localhost:8001
   ```
3. В `.env` выставить `VK_ADAPTER_MODE=playwright`.

Селекторы в `app/adapters/vk.py` поддерживаются вручную по факту разметки VK и не
проверялись живьём в CI/этой реализации — см. plan-parser-service.md, раздел "Тесты".

## Тесты

```bash
python -m venv .venv && .venv\Scripts\activate   # Windows
pip install -r requirements.txt
pytest
```

Все три тестовых файла работают на моках (`respx` мокает HTTP-вызовы к Data Service,
`FakeVkAdapter` — без браузера) и не требуют ни реального VK, ни запущенного Data Service:

- `test_adapters_registry.py` — `NotImplementedAdapter` не падает, режимы `fake`/`playwright`.
- `test_task_status_transitions.py` — переходы `queued→running→done/failed`.
- `test_parse_endpoint_mocked.py` — `POST /parse` целиком, проверка payload `/leads/bulk`.
