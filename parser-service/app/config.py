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

    LOG_LEVEL: str = "INFO"


settings = Settings()
