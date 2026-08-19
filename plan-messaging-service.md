# Детальный план реализации: Messaging Service

> Часть общей архитектуры — см. [architecture.md](architecture.md) (диаграмма, общие конвенции, топология репозитория, антибан-логика VK). Зависит от [`plan-data-service.md`](plan-data-service.md) — Data Service должен быть доведён до зелёного состояния (Фаза 0) прежде чем писать код Отправки.

Фаза 3 общего порядка исполнения (см. таблицу в architecture.md). Реализуется после Parser Service ([`plan-parser-service.md`](plan-parser-service.md)), но не зависит от него напрямую — только от живого Data Service.

## Context

Messaging общается с Data Service только через его REST API (см. [`plan-data-service.md`](plan-data-service.md) — схема, эндпоинты), ничего не переизобретая: атомарная выдача аккаунтов через `/accounts/next-available`, шифрование сессий на стороне Data Service, скользящие rate-limit окна.

Общая структура каталога и конвенции (config, clients/data_service.py, /health, Dockerfile-паттерн с Playwright) — см. раздел "Общие конвенции для Парсера/Отправки/Оркестратора" в [architecture.md](architecture.md). Ниже — специфика именно Messaging Service.

## Структура проекта

```
messaging-service\
├── app\
│   ├── main.py
│   ├── config.py                 # DATA_SERVICE_URL, VK_ADAPTER_MODE(fake|playwright), VK_HEADLESS, MIN_DELAY_SEC, MAX_DELAY_SEC, LOG_LEVEL
│   ├── logging_conf.py
│   ├── clients\data_service.py     # list_leads(status=new,campaign_id,...), next_available_account, get_session, release_account, cooldown_account, patch_lead_status, post_message, get_campaign, get_template
│   ├── schemas\send.py              # SendStatusOut{campaign_id, status, sent, failed, skipped}
│   ├── adapters\
│   │   ├── base.py                  # SendAdapter Protocol: send_message(lead, account, text) -> SendResult
│   │   ├── vk.py                     # VkSendAdapter — playwright.sync_api, purpose=messaging
│   │   ├── dryrun.py                  # DryRunAdapter — общий для tg/instagram и VK_ADAPTER_MODE=fake; логирует "would send", возвращает success
│   │   └── registry.py
│   ├── templating.py                  # render(body, lead) — {{org_name}} и т.п.; выбор A/B-варианта
│   ├── ratelimit.py                    # delay(min,max): random.uniform + sleep между сообщениями в рамках задачи
│   ├── tasks.py                         # in-memory TaskStore (по campaign_id) + run_send_task()
│   ├── routers\send.py                    # POST /campaigns/{id}/send, GET /campaigns/{id}/send-status
│   └── exceptions.py
├── tests\ (по аналогии с Parser: адаптеры, templating A/B, переходы статусов, endpoint на моках)
├── scripts\
│   ├── entrypoint.sh
│   └── vk_manual_login.py              # дублирует parser-service версию — осознанная мелкая дупликация вместо преждевременной абстракции на MVP-масштабе
├── Dockerfile
├── requirements.txt
├── .env.example
└── README.md
```

## Логика `run_send_task(campaign_id)`

`status=running` → пока есть `GET /leads?status=new&campaign_id=...&limit=50` → для каждого лида (только `platform=vk` активен через реальный/fake адаптер, tg/instagram сразу через `DryRunAdapter`):

1. `next_available_account(platform=vk, purpose=messaging, task_ref=campaign_id)` — **новый вызов на каждое сообщение**, не один аккаунт на всю кампанию. Причина: единственный источник правды по лимитам — таблица `messages` в Data Service, и `next-available` пересчитывает `hourly/daily_used` на каждый вызов; если держать один аккаунт всю задачу, легко проскочить лимит между проверками. Цена — лишний запуск Playwright-контекста на сообщение (не логин с нуля — восстановление сессии из `storage_state`, дешевле полноценного логина); переиспользование контекста в пределах `lock_ttl` — явно вне MVP. При `409` (нет свободных аккаунтов) — цикл прерывается, `status=waiting_for_account` (не ошибка).
2. `get_session(account_id)` → `templating.render(template.body, lead)` (A/B — чередование/`random.choice` по `template_variant`).
3. `adapter.send_message(...)`.
4. Успех: `patch_lead_status(lead.id, "contacted")` + `post_message({lead_id, account_id, template_variant, text_sent, delivery_status: sent})`. Это и есть anti-duplicate — `contacted`-лиды больше не попадают в `status=new` выборку.
5. Неуспех/бан-сигнал: `post_message(delivery_status: failed)` + при признаках флуда `cooldown_account(...)`.
6. `ratelimit.delay(min,max)` между сообщениями.

`status=done`, когда `GET /leads?status=new&campaign_id=...` возвращает пусто.

## Известное ограничение

`GET /campaigns/{id}/send-status` отдаёт данные из in-memory счётчиков задачи, а не пересчитывается из Data Service — потому что финализированный эндпоинт `GET /messages?lead_id=&account_id=` не фильтрует по `campaign_id`, а менять контракт Data Service вне рамок принятого плана не предполагается.

## Verification

1. `docker compose up --build` (standalone, `DATA_SERVICE_URL=http://localhost:8001` против уже поднятого Data Service) — `GET http://localhost:8003/health` → `{"status":"ok"}`.
2. `POST /campaigns/{id}/send` с `VK_ADAPTER_MODE=fake` и лидами `status=new` в Data Service — все лиды переходят в `contacted`, для каждого создаётся запись в `messages` с `delivery_status=sent`.
3. Rate-limit: аккаунт с малым `hourly_limit` — после исчерпания лимита `next-available` отдаёт 409, задача переходит в `status=waiting_for_account`, не падает с ошибкой.
4. `pytest` из `messaging-service/` — все тестовые файлы проходят (адаптеры, templating A/B, переходы статусов, endpoint на моках через respx).

## Следующие шаги

После зелёной верификации — Фаза 4: [`plan-orchestrator-service.md`](plan-orchestrator-service.md).
