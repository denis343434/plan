# Рассылка для Instagram

## Контекст

Сейчас в системе (parser-service → data-service → messaging-service, дирижирует orchestrator-service, управляет всем desktop-client) полностью реализована рассылка только для VK: парсер ищет сообщества по ключу через Playwright, messaging-service шлёт им сообщение через тот же Playwright-браузер с сохранённой сессией аккаунта.

Важное открытие по итогам исследования: **система с самого начала спроектирована как мультиплатформенная**, и Instagram уже заведён как первоклассное значение почти everywhere:

- `data-service/app/enums.py::Platform` уже содержит `instagram = "instagram"`, колонка `platform` в `accounts`/`leads`/`campaigns` — обычная строка без CHECK-ограничения на `'vk'`. **Миграции не нужны.**
- `parser-service/app/adapters/registry.py::get_adapter()` и `messaging-service/app/adapters/registry.py::get_adapter()/get_inbox_adapter()` уже диспетчеризуют по `platform`, для всего, что не `"vk"`, отдавая заглушки (`NotImplementedAdapter`, `DryRunAdapter`/`DryRunInboxAdapter`), у которых в докстринге прямо написано "tg/instagram".
- `desktop-client/app.py`: комбобокс платформы при создании кампании (`PLATFORM_LABELS`) уже позволяет выбрать "Instagram" — раньше это тихо уходило в `DryRunAdapter` и ничего не отправляло.
- Протоколы `SourceAdapter` (parser) и `SendAdapter`/`InboxAdapter` (messaging) уже платформо-нейтральны (`Protocol`, dataclass `RawLead`/`SendResult`/`ReplyCheckResult`).

Соответственно, задача — **аддитивная**: написать реальные Instagram-адаптеры по образу и подобию VK-адаптеров (`parser-service/app/adapters/vk.py`, `messaging-service/app/adapters/vk.py`), подключить их в registry, добавить ручной логин и закрыть пару мелких дыр в UI, которые хардкодили VK.

### Решения, принятые пользователем
- **Автоматизация** — Playwright (браузер), архитектурно зеркалим VK-адаптер: тот же `storage_state`, тот же паттерн ручного логина через headful-скрипт.
- **Критерий лида** — поиск аккаунтов по ключевому слову/хэштегу, лид — аккаунт **без сайта в био** (прямой аналог VK-фильтра `has_site=False`).
- **Способ контакта** — просто слать DM как есть, не подписываясь предварительно. Осознанно принимаем риск: сообщение уйдёт в "Запросы" адресата, а также повышенный риск блокировки аккаунта-отправителя за холодные рассылки (Instagram намного агрессивнее VK).
- **Прокси** — пока не подключаем (поле `Account.proxy` остаётся неиспользуемым, как и для VK).

### Шаг 0 — бэкап (по явной просьбе пользователя)
Перед любыми правками сделать полную копию рабочей директории `E:\plan` в отдельную папку с таймстампом (например, `E:\plan_backup_2026-09-18`), исключив `.venv`/`__pycache__`/`.pytest_cache`. Это делается штатным копированием файлов (`robocopy`/`xcopy`), **без git-коммитов** — в репозитории уже есть незакоммиченные изменения (in-progress фича followup), трогать git-историю не нужно, простого снимка папки достаточно как страховки.

---

## Архитектура: что добавляем в каждом сервисе

### 1. parser-service — поиск Instagram-лидов

**Новый файл** `parser-service/app/adapters/instagram.py`:
- `InstagramParserAdapter(SourceAdapter)` — реализует `search_communities(keyword, filters, on_progress) -> list[RawLead]`, как `VkParserAdapter` в `parser-service/app/adapters/vk.py`.
- Запуск: `pw.chromium.launch(headless=settings.IG_HEADLESS)`, `browser.new_context(storage_state=...)` из сессии аккаунта.
- Основной поток поиска (аналог VK-flow "открыть поиск → ввести ключ → результаты"): открыть `instagram.com`, кликнуть по иконке поиска, ввести `keyword` с посимвольной случайной задержкой (как `_type_like_human`/`search_input.type(..., delay=...)` в VK-адаптере), дождаться выпадающей панели результатов, отфильтровать/пролистать раздел с аккаунтами (это прямой аналог "Сообщества" в VK-поиске). Хэштег-поиск (`instagram.com/explore/tags/{tag}/`) держим как запасной путь, если поиск по аккаунтам даёт мало кандидатов — но это отдельный, более тяжёлый режим (там сначала список постов, а автора нужно доставать из каждого поста), в MVP не обязателен.
- Для каждого кандидата: открыть `instagram.com/{username}/`, прочитать поле сайта в био (внешняя ссылка профиля) — если пусто, это лид. `RawLead(external_id=username, group_url=f"https://instagram.com/{username}/", title=<имя аккаунта>, admin_contact=None)`.
- Дедуп через `filters.known_external_ids` — так же, как в `parser-service/app/tasks.py::run_parse_task` уже прокидывается для VK.
- Обработка сбоев по тем же контрактам, что уже есть в `parser-service/app/adapters/base.py` (переиспользуем, не переименовываем):
  - `CaptchaDetectedError` — Instagram показывает форму challenge/подтверждения при подозрительной активности; класс уже общий (лежит в `base.py`, а не в `vk.py`) и принимает `partial_leads`.
  - `SessionExpiredError` — на аналог VK-экрана "Выберите аккаунт для входа" у Instagram будет свой login-wall; поднимаем то же исключение (свой класс внутри `instagram.py`, по тому же принципу, что и `vk.py::SessionExpiredError`).
- Блокировка тяжёлых ресурсов (`_block_heavy_resources`-аналог) для скорости/скрытности — переиспользуем паттерн из `vk.py`.
- **Важная оговорка**: точные CSS/testid-селекторы Instagram нельзя узнать заранее без итерации по живому сайту — у VK-адаптера в коде прямо оставлены комментарии "селекторы проверены вживую"; с Instagram потребуется такой же цикл ручной проверки на реальном аккаунте после первой реализации.

**Изменения в существующих файлах**:
- `parser-service/app/adapters/registry.py::get_adapter()` — ветка `platform == "instagram"`, аналогичная VK (`settings.IG_ADAPTER_MODE == "playwright"` → `InstagramParserAdapter`, иначе `FakeInstagramAdapter` из нового `adapters/instagram_fake.py`).
- `parser-service/app/tasks.py::run_parse_task` — аккаунт/сессия/дедуп-лист брались только в ветке `if platform == "vk":`, обобщено на `if platform in ("vk", "instagram"):`.
- `parser-service/app/config.py` — `IG_ADAPTER_MODE`, `IG_HEADLESS`, `IG_MIN_DELAY_SEC`/`IG_MAX_DELAY_SEC` (более консервативные дефолты, чем у VK), `IG_BIO_CHECK_TIMEOUT_MS`.
- **Новый скрипт** `parser-service/scripts/instagram_manual_login.py` — копия `vk_manual_login.py` с `page.goto("https://www.instagram.com/accounts/login/")`.

### 2. messaging-service — отправка и проверка ответов

**Новый файл** `messaging-service/app/adapters/instagram.py`:
- `InstagramSendAdapter(SendAdapter)` — `send_message(lead, account, text, image=None) -> SendResult`, зеркалит `VkSendAdapter.send_message`: открыть `lead["group_url"]` (профиль), нажать кнопку "Message"/"Написать" (доступна независимо от подписки — сообщение уйдёт в Requests адресата, осознанно принятое поведение), напечатать текст с человеческой задержкой, отправить.
  - `SendResult.flood_detected=True` при экране "Action Blocked"/"Try Again Later".
  - `SessionExpiredError` (`messaging-service/app/adapters/base.py`) при login-wall — переиспользуем как есть.
  - `network_error`/`_PageLoadError` — тот же принцип разделения "временный сбой vs постоянная ошибка", что и в VK.
- `InstagramInboxAdapter(InboxAdapter)` — `check_replies(leads, on_progress)`: открыть `instagram.com/direct/inbox/`, найти диалог с лидом (сопоставление по `external_id`/username, не по отображаемому имени), проверить непрочитанные/входящие.

**Изменения в существующих файлах**:
- `messaging-service/app/adapters/registry.py` — ветка `platform == "instagram" and settings.IG_ADAPTER_MODE == "playwright"` в `get_adapter()` и `get_inbox_adapter()`.
- `messaging-service/app/config.py` — `IG_ADAPTER_MODE`, `IG_HEADLESS`, `IG_MIN_DELAY_SEC`/`IG_MAX_DELAY_SEC`.
- `tasks.py`/`followup.py`/`inbox.py` — менять не нужно, уже полностью платформо-нейтральны.

### 3. desktop-client — закрыть дыры в UI

- `on_create_account` — платформа была захардкожена `"platform": "vk"`. Добавлен комбобокс платформы на панели создания аккаунта (`PLATFORM_LABELS`/`PLATFORM_REVERSE`).
- Исправлена несостыковка `PLATFORM_LABELS["telegram"]` vs `Platform.tg` в data-service enum — ключ приведён к `"tg"`.
- Добавлена кнопка "Войти в Instagram" рядом с "Войти в VK" — та же логика `subprocess.Popen`, указывающая на `instagram_manual_login.py`.
- `on_check_sessions` теперь передаёт `?platform=` в `GET {PARSER_URL}/accounts/{id}/session-check`, чтобы эндпоинт проверял сессию нужным адаптером (эндпоинт в parser-service обобщён под `platform`).

### 4. docker-compose.yml — новые переменные окружения
Добавлены в блоки `parser-service` и `messaging-service` (по аналогии с существующими `VK_*`):
```yaml
IG_ADAPTER_MODE: ${IG_ADAPTER_MODE:-fake}
IG_HEADLESS: ${IG_HEADLESS:-true}
IG_MIN_DELAY_SEC: ${IG_MIN_DELAY_SEC:-3.0}
IG_MAX_DELAY_SEC: ${IG_MAX_DELAY_SEC:-6.0}
```

### 5. data-service
**Изменений не потребовалось** — `Platform.instagram`, схемы аккаунтов/лидов/кампаний уже принимают `platform="instagram"` без миграций.

---

## Риски и как архитектура их уже покрывает

- **Аккаунт-бан из-за холодных DM** — пользователь осознанно выбрал "слать как есть". Единственная мера смягчения на уровне архитектуры — использовать уже существующие `Account.hourly_limit`/`daily_limit`/`warmup_stage` при создании Instagram-аккаунтов (задавать заметно консервативнее, чем для VK, прямо в UI при создании аккаунта).
- **Более агрессивный антибот, чем у VK** — `CaptchaDetectedError`/`SessionExpiredError` уже спроектированы платформо-нейтрально, но именно на Instagram эти ветки будут срабатывать значительно чаще — при первом реальном прогоне ожидать итерацию по селекторам/таймаутам.
- **`orchestrator-service`** — уже полностью платформо-нейтрален (`campaign["platform"]` прокидывается как есть), менять не пришлось; `POLL_STALL_TIMEOUT_SEC` сработает, если Instagram-адаптер надолго перестанет обновлять `progress_checked/found` — `on_progress` callback обязателен к вызову так же часто, как в VK-адаптере.

---

## Порядок реализации
1. Бэкап (шаг 0) — `E:\plan_backup_2026-09-18`.
2. `data-service` — без изменений, только подтверждено, что `platform="instagram"` уже проходит валидацию.
3. `parser-service`: `adapters/instagram_fake.py` → ветка в `registry.py` → ветка в `tasks.py` → `adapters/instagram.py` → `instagram_manual_login.py`.
4. `messaging-service`: `adapters/instagram.py` (send+inbox) → ветка в `registry.py`.
5. `desktop-client`: селектор платформы при создании аккаунта, кнопка логина в Instagram, фикс `telegram`/`tg`, `on_check_sessions`.
6. `docker-compose.yml`: новые env-переменные.

## Проверка
1. **Fake-режим сквозь весь пайплайн** (`IG_ADAPTER_MODE=fake`) — юнит-тесты `test_adapters_registry.py` в обоих сервисах обновлены под новую ветку `instagram`.
2. **Юнит-тесты адаптеров** — `test_instagram_parser_adapter.py` (parser-service), `test_instagram_send_adapter.py` (messaging-service), по образцу существующих тестов на VK (mock Playwright).
3. **Реальный прогон** (ещё предстоит сделать вручную): создать тестовый Instagram-аккаунт, прогнать `instagram_manual_login.py`, запустить парсинг на 1 ключевом слове с малым `max_groups`, сверить, что найденные лиды не имеют сайта в био; затем тестовую рассылку на 1-2 лида с `IG_HEADLESS=false`, визуально проверить, что сообщение реально ушло. На этом шаге ожидается калибровка селекторов под живой Instagram (см. оговорку в разделе parser-service).

## Статус
План реализован: код написан и протестирован в fake-режиме (parser-service 38/38, messaging-service 76/76 тестов проходят). Реальный прогон на живом Instagram-аккаунте (шаг 3 в разделе «Проверка») не выполнялся — потребуется калибровка селекторов, помеченных как непроверенные вживую.
