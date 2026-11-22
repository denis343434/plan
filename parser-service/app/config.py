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
    # Группы, в названии которых есть любая из этих фраз (без учёта регистра и ё/е), парсер
    # отбрасывает сразу, ещё до проверки сайта (см. adapters/vk.py::_is_excluded_title).
    # В .env — JSON-список: VK_EXCLUDED_TITLE_PHRASES=["ищу модель", "ищу моделей"]
    VK_EXCLUDED_TITLE_PHRASES: list[str] = ["ищу модель"]

    IG_ADAPTER_MODE: str = "fake"  # fake | playwright
    IG_HEADLESS: bool = True
    # Instagram банит/чекпоинтит агрессивнее VK — держим более консервативные задержки между
    # проверяемыми кандидатами по умолчанию, чем VK-аналог (MIN_DELAY_SEC/MAX_DELAY_SEC).
    IG_MIN_DELAY_SEC: float = 3.0
    IG_MAX_DELAY_SEC: float = 6.0
    # Таймаут ожидания шапки профиля (см. adapters/instagram.py::_has_bio_link) — по аналогии с
    # VK_SITE_CHECK_TIMEOUT_MS, но своё значение: разметка и скорость рендера Instagram другие.
    IG_BIO_CHECK_TIMEOUT_MS: int = 4000

    # Яндекс Карты — публичные, аккаунт не нужен. headless берётся из VK_HEADLESS (тот же
    # переключатель "Playwright: в фоне/на экране" в панели) — на экране можно руками пройти
    # капчу Яндекса, адаптер её дождётся (YANDEX_CAPTCHA_WAIT_SEC).
    YANDEX_ADAPTER_MODE: str = "fake"  # fake | playwright
    YANDEX_MIN_DELAY_SEC: float = 1.5
    YANDEX_MAX_DELAY_SEC: float = 3.5
    YANDEX_CAPTCHA_WAIT_SEC: int = 180

    # Google Карты — то же самое (adapters/google_maps.py), свои задержки и ожидание капчи.
    GOOGLE_ADAPTER_MODE: str = "fake"  # fake | playwright
    GOOGLE_MIN_DELAY_SEC: float = 1.5
    GOOGLE_MAX_DELAY_SEC: float = 3.5
    GOOGLE_CAPTCHA_WAIT_SEC: int = 180
    # Поиск организаций из Google Карт в Instagram/Facebook (adapters/social_lookup.py): паузы
    # между запросами к соцсети и прокси для Facebook (пусто — напрямую).
    SOCIAL_LOOKUP_MIN_DELAY_SEC: float = 1.5
    SOCIAL_LOOKUP_MAX_DELAY_SEC: float = 3.5
    FB_PROXY: str = ""

    LOG_LEVEL: str = "INFO"


settings = Settings()
