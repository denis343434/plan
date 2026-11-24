"""Разбор контактов организации (ссылки из карточки Яндекс/Google Карт) в лиды по каналам связи.

Отдельно от adapters/yandex_maps.py и adapters/google_maps.py — чистые функции без playwright,
их же использует FakeYandexMapsAdapter и тесты.

external_id каждого канала нормализуется так же, как его записал бы "родной" парсер этой
платформы (VK — короткое имя/club123 из пути, как в adapters/vk.py), — тогда уникальный индекс
(platform, external_id) в Data Service сам не даст написать одной и той же группе дважды, даже
если она уже найдена VK-кампанией.
"""

import re
from urllib.parse import parse_qs, unquote, urlparse

from app.adapters.base import RawLead

CHANNELS = ("vk", "tg", "whatsapp", "facebook", "instagram")
# Источники-карты: у их кампаний платформа источника, а лиды пишутся с платформой канала связи.
MAP_SOURCES = ("yandex_maps", "google_maps")

_VK_HOSTS = {"vk.com", "vk.ru", "m.vk.com", "m.vk.ru", "new.vk.com"}
# Служебные пути VK, которые не являются страницей сообщества/человека (кнопки "поделиться",
# редиректы и т.п.) — по ним писать некому.
_VK_IGNORED = {
    "share.php", "away.php", "wall", "video", "photo", "app", "apps", "feed", "search",
    "login", "join", "im", "write", "doc", "audio", "market", "clips",
}
_VK_SEGMENT = re.compile(r"^(club\d+|public\d+|event\d+|id\d+|[a-zA-Z][a-zA-Z0-9_.]{1,31})$")

_TG_HOSTS = {"t.me", "telegram.me", "www.t.me", "telegram.dog"}
_TG_IGNORED = {"share", "joinchat", "addstickers", "proxy", "socks", "s", "c", "iv", "login"}
_TG_USERNAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{3,31}$")

_WA_HOSTS = {"wa.me", "api.whatsapp.com", "web.whatsapp.com", "whatsapp.com", "www.whatsapp.com", "chat.whatsapp.com"}

_FB_HOSTS = {
    "facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com", "business.facebook.com",
    "fb.com", "www.fb.com", "fb.me", "m.me",
}
# Служебные пути Facebook — не страница организации (кнопки "поделиться", пиксель, логин и т.п.).
_FB_IGNORED = {
    "sharer", "sharer.php", "share", "share.php", "dialog", "plugins", "tr", "login", "login.php",
    "groups", "events", "watch", "photo", "photo.php", "photos", "story.php", "permalink.php",
    "hashtag", "help", "policies", "privacy", "legal", "ads", "business", "messages", "marketplace",
    "reel", "reels", "videos", "home.php", "search", "l.php", "settings", "recover", "pages",
}
_FB_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{1,49}$")

_IG_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com", "instagr.am", "www.instagr.am"}
_IG_IGNORED = {
    "p", "reel", "reels", "tv", "explore", "stories", "accounts", "direct", "about", "legal",
    "developer", "web", "challenge", "privacy", "emails", "session", "s",
}
_IG_USERNAME = re.compile(r"^[A-Za-z0-9._]{1,30}$")

_YANDEX_HOST_MARKERS = ("yandex.", "ya.ru", "yastatic.net", "yandex-team")
# Google Карты отдают внешние ссылки карточки через редиректор google.com/url?q=….
_GOOGLE_HOST_MARKERS = ("google.", "goo.gl", "g.page", "gstatic.com", "googleusercontent.com")
_MAP_HOST_MARKERS = _YANDEX_HOST_MARKERS + _GOOGLE_HOST_MARKERS
# Соцсети в блоке "сайт" карточки — это не собственный сайт (фильтр "без сайта" их не считает).
_SOCIAL_HOST_SUFFIXES = (
    "instagram.com", "ok.ru", "facebook.com", "youtube.com", "dzen.ru", "rutube.ru",
    "tiktok.com", "taplink.cc", "taplink.ru", "max.ru",
    # Бесплатный автосайт Яндекс Бизнеса (проверено вживую 2026-10-03: у многих организаций в
    # поле "сайт" стоит <name>.clients.site) — собственным сайтом не считаем.
    "clients.site",
    # Агрегаторы ссылок и прочие соцсети — тоже не собственный сайт.
    "linktr.ee", "taplink.at", "twitter.com", "x.com", "threads.net", "linkedin.com", "pinterest.com",
    # То же у Google: бесплатные сайты Google Business Profile.
    "business.site",
)
# Собственные каналы Яндекс Карт в подвале каждой карточки (проверено вживую 2026-10-03:
# "card-social-links-view__link" → https://t.me/mapsyandex) — не контакт организации.
_IGNORED_ACCOUNTS = {"mapsyandex", "yandexmaps", "yandex", "yandex.maps"}


def _unwrap(href: str) -> str:
    """Яндекс (?url=/&u=) и Google (/url?q=) иногда отдают внешние ссылки через редиректор —
    достаём настоящую."""
    parsed = urlparse(href)
    if any(m in (parsed.hostname or "") for m in _MAP_HOST_MARKERS):
        qs = parse_qs(parsed.query)
        for key in ("url", "u", "to", "q"):
            if qs.get(key):
                return unquote(qs[key][0])
    return href


def _host(parsed) -> str:
    host = (parsed.hostname or "").lower()
    return host[4:] if host.startswith("www.") and host[4:] in _VK_HOSTS | _TG_HOSTS else host


def phone_digits(raw: str | None) -> str | None:
    """'+7 (999) 123-45-67' / '8 999 1234567' → '79991234567'. None, если это не похоже на номер."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 10 and digits.startswith("9"):
        digits = "7" + digits
    return digits if 10 <= len(digits) <= 15 else None


# Мобильные префиксы стран, для которых кампании реально создаются (Яндекс/Google Карты
# поддерживают выбор страны). WhatsApp по номеру имеет смысл только для мобильных: городские
# (+7 4xx/8xx, +7 727, +375 17, +380 44…) в WhatsApp почти не заведены, поэтому не угадываем
# по любому телефону. Ключ — код страны, значения — допустимые префиксы национального номера.
_WHATSAPP_MOBILE_PREFIXES: dict[str, tuple[str, ...]] = {
    # Россия (+7 9xx) и Казахстан (+7 70x/747/771/775-778); городские +7 727 и т.п. не входят.
    "7": (
        "9", "700", "701", "702", "703", "704", "705", "706", "707", "708", "747",
        "750", "751", "760", "761", "762", "763", "771", "775", "776", "777", "778",
    ),
    "375": ("25", "29", "33", "44"),  # Беларусь
    "380": ("39", "50", "63", "66", "67", "68", "73", "89", "9"),  # Украина
    "998": ("9",),  # Узбекистан
    "996": ("5", "7", "9"),  # Кыргызстан
    "994": ("5", "7"),  # Азербайджан
    "374": ("4", "5", "7", "9"),  # Армения
    "995": ("5",),  # Грузия
    "90": ("5",),  # Турция
    "971": ("5",),  # ОАЭ
}


def is_mobile_for_whatsapp(digits: str | None) -> bool:
    """Похож ли номер на мобильный, у которого может быть WhatsApp.

    Раньше проверялись только российские +7 9xx, из-за чего в кампаниях по Казахстану
    (+7 70x/77x) WhatsApp по номеру не срабатывал вообще, хотя номер мобильный.
    """
    if not digits or not 10 <= len(digits) <= 15:
        return False
    for code, prefixes in _WHATSAPP_MOBILE_PREFIXES.items():
        if digits.startswith(code):
            national = digits[len(code):]
            return any(national.startswith(prefix) for prefix in prefixes)
    return False


def vk_lead(href: str) -> RawLead | None:
    parsed = urlparse(_unwrap(href))
    if _host(parsed) not in _VK_HOSTS:
        return None
    segment = parsed.path.strip("/").split("/", 1)[0]
    if (
        not segment
        or segment.lower() in _VK_IGNORED | _IGNORED_ACCOUNTS
        or not _VK_SEGMENT.match(segment)
    ):
        return None
    return RawLead(external_id=segment, group_url=f"https://vk.com/{segment}", platform="vk")


def tg_lead(href: str) -> RawLead | None:
    parsed = urlparse(_unwrap(href))
    if parsed.scheme == "tg":
        qs = parse_qs(parsed.query)
        domain = (qs.get("domain") or [""])[0]
        return _tg_from_username(domain)
    if _host(parsed) not in _TG_HOSTS:
        return None
    segment = parsed.path.strip("/").split("/", 1)[0]
    if segment.startswith("+"):
        digits = phone_digits(segment)
        # t.me/+79991234567 — чат по номеру; t.me/+AbCdEf — инвайт в группу (писать некому).
        if digits and segment[1:].isdigit():
            return RawLead(external_id=f"+{digits}", group_url=f"https://t.me/+{digits}", platform="tg")
        return None
    return _tg_from_username(segment)


def _tg_from_username(segment: str) -> RawLead | None:
    if (
        not segment
        or segment.lower() in _TG_IGNORED | _IGNORED_ACCOUNTS
        or not _TG_USERNAME.match(segment)
    ):
        return None
    username = segment.lower()
    return RawLead(external_id=username, group_url=f"https://t.me/{username}", platform="tg")


def whatsapp_lead(href: str) -> RawLead | None:
    parsed = urlparse(_unwrap(href))
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "whatsapp":
        digits = phone_digits((parse_qs(parsed.query).get("phone") or [""])[0])
    elif host in _WA_HOSTS:
        if host == "chat.whatsapp.com":
            return None  # инвайт в группу — не личный чат организации
        path = parsed.path.strip("/")
        if path.startswith("message/"):
            return None  # короткая ссылка каталога WA Business, номер из неё не достать
        digits = phone_digits((parse_qs(parsed.query).get("phone") or [""])[0]) or phone_digits(path)
    else:
        return None
    return whatsapp_lead_from_phone(digits)


def facebook_lead(href: str) -> RawLead | None:
    """facebook.com/<page>, m.me/<page>, profile.php?id=<id>, /people/<name>/<id>, /pages/<name>/<id>
    → external_id: ник страницы в нижнем регистре или числовой id."""
    parsed = urlparse(_unwrap(href))
    host = (parsed.hostname or "").lower()
    if host not in _FB_HOSTS:
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        return None
    first = parts[0].lower()
    if first == "profile.php":
        page_id = (parse_qs(parsed.query).get("id") or [""])[0]
        return _fb_lead(page_id) if page_id.isdigit() else None
    if first in ("people", "pages") and len(parts) >= 3 and parts[2].isdigit():
        return _fb_lead(parts[2])
    if first in _FB_IGNORED | _IGNORED_ACCOUNTS or not _FB_SEGMENT.match(parts[0]):
        return None
    return _fb_lead(parts[0].lower())


def _fb_lead(page: str) -> RawLead:
    return RawLead(external_id=page, group_url=f"https://www.facebook.com/{page}", platform="facebook")


def tg_lead_from_phone(digits: str | None) -> RawLead | None:
    """Telegram по номеру из карточки (только в международном виде: Google Карты так и отдают
    номера не из РФ, "+44 20 …"; национальный "0…" без кода страны не годится). Тот же вид, что
    t.me/+<номер> в tg_lead — dedup общий. Написать получится, только если номер есть в Telegram
    и не скрыт приватностью; иначе отправка вернёт "пользователь не найден"."""
    if not digits or digits.startswith("0"):
        return None
    return RawLead(external_id=f"+{digits}", group_url=f"https://t.me/+{digits}", platform="tg")


def instagram_lead(href: str) -> RawLead | None:
    parsed = urlparse(_unwrap(href))
    if (parsed.hostname or "").lower() not in _IG_HOSTS:
        return None
    segment = parsed.path.strip("/").split("/", 1)[0]
    if not segment or segment.lower() in _IG_IGNORED | _IGNORED_ACCOUNTS or not _IG_USERNAME.match(segment):
        return None
    return instagram_lead_from_username(segment)


def instagram_lead_from_username(username: str) -> RawLead:
    # Тот же вид, что у parser-service/app/adapters/instagram.py — dedup общий.
    username = username.lower()
    return RawLead(external_id=username, group_url=f"https://www.instagram.com/{username}/", platform="instagram")


def whatsapp_lead_from_phone(digits: str | None) -> RawLead | None:
    if not digits:
        return None
    return RawLead(external_id=digits, group_url=f"https://wa.me/{digits}", platform="whatsapp")


_PARSERS = {"vk": vk_lead, "tg": tg_lead, "whatsapp": whatsapp_lead, "facebook": facebook_lead, "instagram": instagram_lead}


def channel_leads(hrefs: list[str]) -> dict[str, RawLead]:
    """Все найденные каналы связи организации: {"vk": RawLead, ...} — по первой подходящей ссылке."""
    found: dict[str, RawLead] = {}
    for href in hrefs:
        for channel, parse in _PARSERS.items():
            if channel in found:
                continue
            lead = parse(href)
            if lead is not None:
                found[channel] = lead
                break
    return found


def pick_lead(
    found: dict[str, RawLead],
    channels: tuple[str, ...],
    known_keys: frozenset[str] = frozenset(),
) -> RawLead | None:
    """Один лид на организацию — первый канал из channels (порядок = приоритет), по которому есть
    контакт. Если этот контакт уже есть в базе (known_keys — "platform:external_id"), организации
    уже писали или она уже в очереди — второй раз через другой мессенджер не пишем."""
    for channel in channels:
        lead = found.get(channel)
        if lead is None:
            continue
        if f"{lead.platform}:{lead.external_id}" in known_keys:
            return None
        return lead
    return None


def is_external_site(href: str) -> bool:
    """Ссылка на собственный сайт организации — не соцсеть/мессенджер и не сами карты."""
    parsed = urlparse(_unwrap(href))
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        return False
    if any(m in host for m in _MAP_HOST_MARKERS):
        return False
    if host in _VK_HOSTS | _TG_HOSTS | _WA_HOSTS | _FB_HOSTS:
        return False
    return not any(host == s or host.endswith("." + s) for s in _SOCIAL_HOST_SUFFIXES)
