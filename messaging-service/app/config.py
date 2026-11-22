from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATA_SERVICE_URL: str = "http://localhost:8001"

    VK_ADAPTER_MODE: str = "fake"  # fake | playwright
    VK_HEADLESS: bool = True
    MIN_DELAY_SEC: float = 1.0
    MAX_DELAY_SEC: float = 3.0
    # MIN_DELAY_SEC/MAX_DELAY_SEC (и IG_-аналоги) — ЕДИНСТВЕННАЯ пауза между лидами, её держит
    # tasks.py/followup.py (ratelimit.delay_for_platform). Внутри send_message адаптеров раньше
    # стояло ещё три таких же паузы (после перехода в чат, перед вводом, после отправки) — по
    # факту 4 паузы на сообщение вместо одной. Там теперь только короткое "дать странице
    # устояться" с этим диапазоном.
    SEND_SETTLE_MIN_SEC: float = 0.3
    SEND_SETTLE_MAX_SEC: float = 0.8
    # Задержка между нажатиями при "человеческом" наборе текста (раньше жёстко 30-120мс на символ,
    # +7-15с на сообщение в 150 символов). 0/0 — набор без задержек.
    TYPING_DELAY_MIN_MS: float = 10.0
    TYPING_DELAY_MAX_MS: float = 40.0
    # Сколько браузеров (по одному на аккаунт) рассылка держит открытыми одновременно — при
    # ротации большого пула аккаунтов самый давно неиспользованный закрывается (каждый headless
    # Chromium — это ~150-300МБ памяти).
    SEND_MAX_OPEN_BROWSERS: int = 3
    # Сколько дней ждать ответа от лида (status=contacted, ни одного sent-сообщения с
    # reply_status=replied) прежде чем послать ему повторное сообщение — см. app/followup.py.
    FOLLOWUP_DELAY_DAYS: float = 3.0
    INBOX_CHECK_CONCURRENCY: int = 3
    # Раньше здесь был gate по ОБЩЕМУ бейджу непрочитанных в левом меню VK (#l_msg) — дважды
    # ловил ложное срабатывание вживую (не находил реальный ответ от "Кафе Миндаль"). Позже
    # выяснилось, что настоящая причина пропущенных ответов была вообще не в бейдже, а в баге
    # параллельного обхода (общий Page на несколько потоков падал с Playwright-ошибкой на КАЖДОМ
    # лиде — см. git-историю vk.py) — так что сам бейдж мог быть ни при чём, просто совпало.
    # По запросу пользователя 2026-08-21 заменили его на встроенный тумблер VK "Только
    # непрочитанные" на странице списка диалогов (vk.com/im) — см. VkInboxAdapter.
    # _walk_unread_dialogs. Изначально список диалогов сопоставлялся с лидами по тексту
    # заголовка — оказалось ненадёжно (лог 2026-08-23: "matched 2/62", реальные ответы
    # молча терялись); 2026-08-23 переделали на прямой клик по каждому диалогу из списка и
    # сопоставление по href автора (тот же признак, что и в основном обходе _check_one).
    # Включено по умолчанию: подтверждено вживую 2026-08-21 и на отрицательном случае
    # (0 совпадений среди лидов без ответа), и на положительном (реальный новый ответ найден
    # и не пропущен).
    INBOX_CHECK_FILTER_BY_UNREAD_LIST: bool = True

    # Сколько ждать поле "официальный сайт сообщества" на странице группы перед отправкой — см.
    # VkSendAdapter._has_external_site. НЕ то же самое, что VK_SITE_CHECK_TIMEOUT_MS в
    # parser-service/app/config.py (там 4000мс, ждать рендер страницы "с нуля"): здесь проверка
    # идёт уже ПОСЛЕ того, как на этой же странице нашлась кнопка "Написать сообщение" — то есть
    # страница гарантированно готова, короткий таймаут тут не риск ложного "сайта нет", а просто
    # подстраховка на случай, если сайт в шапке отрисовывается на мгновение позже остального.
    # Полноценный parser-service-таймаут здесь бы стоил лишние ~4с НА КАЖДОЕ сообщение без сайта
    # (то есть почти на каждое — задача этой проверки ловить только ошибки парсера) и на
    # кампании из сотни лидов реально выталкивал рассылку за POLL_TIMEOUT_SEC оркестратора
    # (см. запрос пользователя 2026-08-25, "фаза: таймаут").
    VK_SITE_CHECK_TIMEOUT_MS: int = 500
    # Прокси для всех VK-браузеров messaging-service: "http://login:pass@host:port". Пусто — без
    # прокси. Задаётся только через .env (не коммитится) — в коде/примерах креды не хранить.
    VK_PROXY: str = ""

    IG_ADAPTER_MODE: str = "fake"  # fake | playwright
    IG_HEADLESS: bool = True
    # Instagram банит/чекпоинтит агрессивнее VK — держим более консервативные задержки по
    # умолчанию, чем VK-аналог (MIN_DELAY_SEC/MAX_DELAY_SEC).
    IG_MIN_DELAY_SEC: float = 3.0
    IG_MAX_DELAY_SEC: float = 6.0

    # Telegram Web / WhatsApp Web (лиды из Яндекс Карт, см. adapters/web_messenger.py). headless —
    # общий VK_HEADLESS (переключатель "Playwright" в панели). Паузы между лидами длиннее VK:
    # оба мессенджера банят за массовые сообщения незнакомым контактам быстрее.
    TG_ADAPTER_MODE: str = "fake"  # fake | playwright
    TG_MIN_DELAY_SEC: float = 5.0
    TG_MAX_DELAY_SEC: float = 12.0
    # Прокси для Telegram-браузеров: "http://login:pass@host:port" (socks5:// тоже, но без
    # логина/пароля — так их поддерживает Chromium). Пусто — без прокси. Тот же адрес читает
    # скрипт входа parser-service/scripts/messenger_manual_login.py — у аккаунта не должен
    # прыгать IP между входом и рассылкой. Только через .env, креды в коде не хранить.
    TG_PROXY: str = ""
    WA_ADAPTER_MODE: str = "fake"  # fake | playwright
    WA_MIN_DELAY_SEC: float = 10.0
    WA_MAX_DELAY_SEC: float = 25.0
    # Facebook Messenger (лиды из Google Карт, FacebookSendAdapter в adapters/web_messenger.py).
    FB_ADAPTER_MODE: str = "fake"  # fake | playwright
    FB_MIN_DELAY_SEC: float = 10.0
    FB_MAX_DELAY_SEC: float = 25.0
    # Facebook в РФ заблокирован — без прокси не откроется. Пусто — берётся TG_PROXY (он и так
    # зарубежный); формат и правила те же, что у TG_PROXY.
    FB_PROXY: str = ""

    LOG_LEVEL: str = "INFO"


settings = Settings()
