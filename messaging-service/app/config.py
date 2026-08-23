from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATA_SERVICE_URL: str = "http://localhost:8001"

    VK_ADAPTER_MODE: str = "fake"  # fake | playwright
    VK_HEADLESS: bool = True
    MIN_DELAY_SEC: float = 1.0
    MAX_DELAY_SEC: float = 3.0
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

    LOG_LEVEL: str = "INFO"


settings = Settings()
