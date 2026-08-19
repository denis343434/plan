from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str
    SESSION_ENCRYPTION_KEY: str
    DEFAULT_LOCK_TTL_SECONDS: int = 900
    LOG_LEVEL: str = "INFO"


settings = Settings()
