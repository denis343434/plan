from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATA_SERVICE_URL: str = "http://localhost:8001"
    PARSER_SERVICE_URL: str = "http://localhost:8002"
    MESSAGING_SERVICE_URL: str = "http://localhost:8003"

    POLL_INTERVAL_SEC: float = 5.0
    # НЕ общий потолок на всю фазу (парсинг/рассылка большой кампании и так легитимно занимает
    # больше получаса — см. _poll в orchestration.py) — это порог, после которого статус фазы
    # считается зависшим, если он вообще перестал меняться. Живая, но медленная кампания
    # (см. запрос пользователя 2026-09-03, скриншот "ошибка: messaging: running" на кампании
    # ещё в процессе) больше не ставится на паузу только за то, что заняла много времени.
    POLL_STALL_TIMEOUT_SEC: float = 1800.0

    LOG_LEVEL: str = "INFO"


settings = Settings()
