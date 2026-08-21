# Messaging Service

Отправляет сообщения лидам, найденным [Parser Service](../parser-service) и сохранённым в
[Data Service](../data-service) (`status=new`). Не хранит своих данных — своей БД нет, статус
задач рассылки держится в памяти процесса, по одной задаче на `campaign_id`.

Подробности решений — [`../plan-messaging-service.md`](../plan-messaging-service.md),
общая архитектура и антибан-логика VK — [`../architecture.md`](../architecture.md).

## Быстрый старт

Data Service должен быть уже поднят (см. `../data-service/README.md`), в нём должны быть
заведены кампания (`POST /campaigns`) и хотя бы один шаблон (`POST /templates`) с
`template_id`, привязанным к кампании (`PATCH /campaigns/{id}`).

```bash
cp .env.example .env
# .env по умолчанию: VK_ADAPTER_MODE=fake — не требует реального VK-аккаунта.

docker build -t messaging-service .
docker run --rm -p 8003:8000 --env-file .env \
  -e DATA_SERVICE_URL=http://host.docker.internal:8001 \
  messaging-service
```

> Отдельного `docker-compose.yml` у Messaging Service нет — по решению из `architecture.md`
> ("Compose-топология"), один compose только у Data Service для его изолированной разработки;
> остальные три сервиса собираются/запускаются как обычные Docker-образы до появления
> корневого `docker-compose.yml` (Фаза 5).

```bash
curl http://localhost:8003/health
# {"status":"ok"}
```

## Пример запроса

```bash
curl -X POST http://localhost:8003/campaigns/{campaign_id}/send
# 202 {"campaign_id":"...","status":"queued","sent":0,"failed":0,"skipped":0,"error":null}

curl http://localhost:8003/campaigns/{campaign_id}/send-status
# {"campaign_id":"...","status":"done","sent":3,"failed":0,"skipped":0,"error":null}
```

Повторный `POST .../send`, пока задача ещё `running`, не запускает вторую параллельную
задачу — возвращает текущий статус.

## Логика рассылки

Для каждого лида со `status=new` в кампании — новый вызов `POST /accounts/next-available`
(`purpose=messaging`) на **каждое сообщение**, а не один аккаунт на всю задачу — единственный
источник правды по лимитам это таблица `messages` в Data Service, и `next-available`
пересчитывает `hourly/daily_used` на каждый вызов. При `409` (нет свободных аккаунтов) —
задача останавливается, `status=waiting_for_account` (не ошибка).

С `VK_ADAPTER_MODE=fake` (по умолчанию) задача всё равно проходит через реальные
`POST /accounts/next-available` → `GET /accounts/{id}/session` → `POST /accounts/{id}/release`
в Data Service для **любой** платформы — `DryRunAdapter` (общий для `VK_ADAPTER_MODE=fake` и
для `tg`/`instagram`) просто не открывает браузер и не шлёт реальное сообщение, но реальная
locking-логика и лимиты проверяются по-настоящему.

Шаблон для рендера ищется так: сначала `GET /templates` (без фильтра — Data Service не
поддерживает `campaign_id` на этом эндпоинте) и клиентская фильтрация по `campaign_id` — если
под кампанию заведено несколько шаблонов с разными `variant` (A/B), между ними идёт
`random.choice` на каждое сообщение; если ни одного не нашлось — используется единственный
`campaign.template_id` через `GET /templates/{id}`. Если и его нет — задача сразу
`status=failed`.

При флуд-сигнале от адаптера (капча/ограничение) — `POST /accounts/{id}/cooldown` вместо
`release`, чтобы не сбросить статус обратно в `active`.

## Входящие сообщения (проверка ответов)

Асинхронно, как и рассылка (`POST .../send` + `GET .../send-status`) — один обход всех
переписок в реальном браузере может занять минуты, блокирующий запрос на всё это время выглядел
бы для клиента как зависание:

```bash
curl -X POST "http://localhost:8003/inbox/check?account_id={account_id}"
# 202 {"account_id":"...","status":"queued","checked":0,"total":0,"replied":0,"error":null,"results":[]}

curl "http://localhost:8003/inbox/check-status?account_id={account_id}"
# {"account_id":"...","status":"running","checked":4,"total":12,"replied":1,"error":null,"results":[]}
# ...затем status:"done", results заполнен целиком
```

Повторный `POST .../check`, пока задача ещё `running`, не запускает вторую параллельную
задачу — возвращает текущий прогресс.

Задача берёт до `limit` (по умолчанию 20) отправленных этим аккаунтом сообщений, у которых ещё
нет `reply_status=replied`, открывает переписку с каждым лидом в одном браузерном контексте
(`VkInboxAdapter.check_replies` в `app/adapters/vk.py`, прогресс — `checked`/`total` в задаче
растут по ходу) и смотрит, есть ли новое входящее сообщение. При находке — `PATCH
/messages/{id}/reply` (`reply_status=replied`, `reply_preview`) и `PATCH /leads/{id}/status`
(`status=replied`) в Data Service; если ответа нет — `reply_status=no_reply` (перепроверяется
при следующем вызове, вдруг ответ придёт позже).

С `VK_ADAPTER_MODE=fake` (по умолчанию) используется `DryRunInboxAdapter` — всегда
`has_reply=false`, реальный VK не открывается.

**Не проверено вживую** (в отличие от адаптера отправки — см. следующий раздел): селекторы
для чтения переписки (`_MESSAGE_BUBBLE_SELECTORS` в `app/adapters/vk.py`) и эвристика
"своё/чужое сообщение" по выравниванию — первая проверка и правка по факту нужна на реальном
залогиненном аккаунте. Таймауты на эти селекторы намеренно короткие (один
`wait_for_selector` на все кандидаты сразу, ≤6с) именно потому, что они непроверенные — пока
не подтверждены живьём, лучше быстро сдаться на лида и пойти к следующему, чем ждать полный
таймаут на каждый из трёх кандидатов по очереди.

## Реальный VK (Playwright)

1. Создать аккаунт в Data Service (`POST /accounts`, `purpose=messaging` или `both`).
2. Один раз залогиниться вручную и сохранить сессию:
   ```bash
   python scripts/vk_manual_login.py --account-id <uuid> --data-service-url http://localhost:8001
   ```
3. В `.env` выставить `VK_ADAPTER_MODE=playwright`.

Селекторы в `app/adapters/vk.py` поддерживаются вручную по факту разметки VK и не
проверялись живьём в CI/этой реализации — см. `plan-messaging-service.md`, раздел "Verification".

## Известные ограничения

- `GET /campaigns/{id}/send-status` отдаёт данные из in-memory счётчиков задачи, а не
  пересчитывается из Data Service — у `GET /messages` нет фильтра по `campaign_id`.
- Поле `skipped` в ответе присутствует для полноты схемы (см. `plan-messaging-service.md`), но
  в MVP не инкрементируется ни одним путём кода — при текущем наборе шаблонов и заглушек tg/
  instagram (`DryRunAdapter` всегда возвращает успех) естественного случая для пропуска лида
  без ошибки не возникает.

## Тесты

```bash
python -m venv .venv && .venv\Scripts\activate   # Windows
pip install -r requirements.txt
pytest
```

Все тестовые файлы работают на моках (`respx` мокает HTTP-вызовы к Data Service,
`DryRunAdapter`/подменённый адаптер — без браузера) и не требуют ни реального VK, ни
запущенного Data Service:

- `test_adapters_registry.py` — выбор `DryRunAdapter`/`VkSendAdapter` и
  `DryRunInboxAdapter`/`VkInboxAdapter` по `platform`/`VK_ADAPTER_MODE`.
- `test_templating.py` — подстановка плейсхолдеров, `random.choice` между A/B-вариантами.
- `test_task_status_transitions.py` — переходы `queued→running→done/waiting_for_account/failed`,
  флуд-сигнал → `cooldown` вместо `release`.
- `test_send_endpoint_mocked.py` — `POST /campaigns/{id}/send` + `GET .../send-status` целиком,
  повторный `POST` во время `running` не дублирует задачу.
- `test_inbox_endpoint.py` — `POST /inbox/check` + `GET /inbox/check-status`: без ответа →
  `reply_status=no_reply`, с найденным ответом (через подменённый адаптер) →
  `reply_status=replied` + `leads.status=replied`, пустой список сообщений — без похода за
  сессией, повторный `POST` во время `running` не дублирует задачу, статус для неизвестного
  `account_id` — 404.
