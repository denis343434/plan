from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATA_SERVICE_URL: str = "http://localhost:8001"
    PARSER_SERVICE_URL: str = "http://localhost:8002"
    MESSAGING_SERVICE_URL: str = "http://localhost:8003"

    POLL_INTERVAL_SEC: float = 5.0
    POLL_TIMEOUT_SEC: float = 1800.0

    LOG_LEVEL: str = "INFO"


settings = Settings()
