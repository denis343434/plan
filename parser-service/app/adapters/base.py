from dataclasses import dataclass
from typing import Callable, Protocol


@dataclass
class ParseFilters:
    has_site: bool | None = None
    # Верхняя граница числа кандидатов, которых стоит проверять/возвращать за один прогон
    # поиска — None означает "без ограничения" (берём всё, что нашлось на странице выдачи).
    max_groups: int | None = None
    # external_id уже известных лидов (глобально по platform, см. uq_leads_platform_external_id
    # в Data Service) — адаптер пропускает такие карточки и докручивает выдачу дальше вместо
    # того, чтобы каждый раз находить один и тот же верхний срез результатов поиска VK.
    known_external_ids: frozenset[str] = frozenset()
    # Только для yandex_maps (см. adapters/yandex_maps.py): страна и город поиска, каналы связи в
    # порядке приоритета (по одному лиду на организацию — первый найденный канал из списка) и
    # писать ли в WhatsApp по мобильному номеру из карточки, если явной ссылки на мессенджер нет.
    # country необязателен: без него запрос остаётся прежним «вид деятельности + город».
    city: str | None = None
    country: str | None = None
    channels: tuple[str, ...] = ("vk", "tg", "whatsapp")  # + "facebook" (Google Карты)
    whatsapp_by_phone: bool = False
    # Только google_maps: если в карточке нет ссылки на канал — Telegram по номеру телефона,
    # Instagram/Facebook поиском по названию со сверкой (adapters/social_lookup.py).
    search_socials: bool = False


@dataclass
class RawLead:
    external_id: str
    group_url: str
    admin_contact: str | None = None
    title: str | None = None
    # Платформа самого лида, если она не совпадает с платформой задачи парсинга — у yandex_maps
    # источник один, а лиды пишутся с платформой канала связи (vk/tg/whatsapp), чтобы рассылка
    # выбирала адаптер и аккаунт по lead["platform"] как обычно. None — платформа задачи.
    platform: str | None = None


class SourceAdapter(Protocol):
    def search_communities(
        self,
        keyword: str,
        filters: ParseFilters,
        on_progress: Callable[[int, int, int], None] | None = None,
    ) -> list[RawLead]: ...


class CaptchaDetectedError(Exception):
    """Raised by an adapter when VK anti-bot/captcha is detected mid-scrape.

    Carries whatever leads were already collected so the caller can still
    persist a partial result instead of losing it.
    """

    def __init__(self, message: str, partial_leads: list[RawLead] | None = None) -> None:
        super().__init__(message)
        self.partial_leads = partial_leads or []


class AntiBotChallengeError(CaptchaDetectedError):
    """VK показал страницу проверки "Проверяем, что вы не робот" (challenge.html).

    Это НЕ протухшая сессия и не капча на самом аккаунте — блокировка привязана к IP/браузеру
    и снимается ручным нажатием "Продолжить". Поэтому вызывающий код не должен отправлять
    аккаунт в кулдаун (см. app/tasks.py) — иначе каждая попытка парсинга "вешает" рабочий
    аккаунт на 30 минут, хотя с ним всё в порядке.
    """


class BrowserCrashedError(Exception):
    """Raised by an adapter when the browser or the Playwright driver itself died mid-scrape.

    Carries whatever leads were already collected so the caller can still
    persist a partial result instead of losing it.
    """

    def __init__(self, message: str, partial_leads: list[RawLead] | None = None) -> None:
        super().__init__(message)
        self.partial_leads = partial_leads or []
