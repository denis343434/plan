from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATA_SERVICE_URL: str = "http://localhost:8001"

    VK_ADAPTER_MODE: str = "fake"  # fake | playwright
    VK_HEADLESS: bool = True
    MIN_DELAY_SEC: float = 1.0
    MAX_DELAY_SEC: float = 3.0
    # Таймаут ожидания поля "сайт" на странице группы (см. adapters/vk.py). Страница к этому
    # моменту уже domcontentloaded, так что это не старт SPA с нуля, а докрутка одного блока —
    # 15с здесь означало, что КАЖДАЯ группа без сайта (то есть большинство — цель фильтра
    # has_site=False) простаивала полный таймаут, прежде чем adapter решал, что сайта нет.
    VK_SITE_CHECK_TIMEOUT_MS: int = 4000

    # Пауза перед тем, как аккаунт снова станет доступен next_available_account() для нового
    # цикла парсинга (см. tasks.py). Без неё единственный парсинг-аккаунт тут же выдаётся
    # повторно (сортировка кандидатов по last_used_at) — на практике 5 циклов подряд с
    # интервалом ~2 мин на одном аккаунте уже ловили мягкий троттлинг VK (поиск переставал
    # отдавать карточки на несколько минут, явной капчи при этом не было, поэтому штатная
    # ветка CaptchaDetectedError его не ловит). Уменьшено с 5 до 2 мин по явному запросу —
    # это ровно тот интервал, что спровоцировал троттлинг 2026-08-21, так что запас минимальный;
    # если троттлинг повторится, первое, что стоит проверить — снова увеличить это значение.
    # Применяется только к реальному playwright-адаптеру (в fake-режиме VK не трогаем).
    VK_PARSE_COOLDOWN_MIN: int = 2

    LOG_LEVEL: str = "INFO"


settings = Settings()
