from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATA_SERVICE_URL: str = "http://localhost:8001"

    VK_ADAPTER_MODE: str = "fake"  # fake | playwright
    VK_HEADLESS: bool = True
    MIN_DELAY_SEC: float = 1.0
    MAX_DELAY_SEC: float = 3.0
    INBOX_CHECK_CONCURRENCY: int = 3

    LOG_LEVEL: str = "INFO"


settings = Settings()
