"""Сверка "это та же организация?" между карточкой Google Карт и найденным профилем в
Instagram / Facebook — чистые функции без playwright (их же гоняют тесты).

Правило совпадения (см. Match.ok): название должно совпасть И хотя бы одно из — телефон или адрес.
Одного названия мало: по "Naru London" Instagram отдаёт пять разных "Naru" (проверено вживую
2026-10-04 — среди них ресторан в Гринвиче по другому адресу). Фото не сравниваем: в карточке
Google обычно интерьер/еда, в профиле — логотип, совпадения картинок почти не бывает.
"""

import re
from dataclasses import dataclass, field

# Слова, по которым организации не отличить друг от друга — не участвуют в сверке названия.
_GENERIC_NAME_WORDS = {
    "restaurant", "ресторан", "cafe", "кафе", "coffee", "кофейня", "bar", "бар", "the", "and",
    "london", "moscow", "москва", "salon", "салон", "studio", "студия", "shop", "магазин", "ltd",
    "llc", "ооо", "ип", "official", "kitchen", "кухня", "house", "group", "center", "центр",
}
_GENERIC_STREET_WORDS = {
    "street", "road", "avenue", "lane", "square", "place", "drive", "court", "close", "way", "hill",
    "улица", "проспект", "переулок", "шоссе", "бульвар", "площадь", "набережная", "проезд",
    "микрорайон", "дом", "корпус", "строение", "russia", "россия", "великобритания", "kingdom",
    "united",
}
# Номера телефонов в тексте профиля: "+44 20 7379 7962", "02073797962", "8 (495) 626-02-82".
_PHONE_RUN_RE = re.compile(r"\+?\d[\d\s().\-]{5,}\d")
# Сравниваем по хвосту номера: код страны в профиле часто опущен ("0207…" вместо "+44 207…").
_PHONE_TAIL = 9


def _words(text: str) -> list[str]:
    return re.findall(r"[0-9a-zа-яё]+", text.lower())


def _compact(text: str) -> str:
    return "".join(_words(text))


def name_tokens(name: str) -> list[str]:
    return [w for w in _words(name) if len(w) >= 3 and w not in _GENERIC_NAME_WORDS]


def name_matches(org_names: list[str], *candidate_texts: str) -> bool:
    """Все значимые слова хотя бы одного из названий организации встречаются в нике/имени профиля
    (ник сравниваем слитно: "narurestaurantlndn" содержит "naru")."""
    haystack = " ".join(_compact(t) for t in candidate_texts if t)
    for name in org_names:
        tokens = name_tokens(name)
        if tokens and all(t in haystack for t in tokens):
            return True
    return False


def phone_matches(phone_digits: str | None, text: str) -> bool:
    if not phone_digits or len(phone_digits) < 7:
        return False
    tail = phone_digits[-_PHONE_TAIL:]
    for run in _PHONE_RUN_RE.findall(text):
        digits = re.sub(r"\D", "", run)
        if len(digits) >= 7 and digits.endswith(tail):
            return True
    return False


def address_matches(address: str | None, text: str) -> bool:
    """Улица (значимое слово) + номер дома, или почтовый индекс целиком."""
    if not address:
        return False
    low = text.lower()
    parts = [p.strip() for p in address.split(",") if p.strip()]
    # Индекс: "WC2H 8EG" (часть с буквами и цифрами после города) или "420111".
    for part in parts[1:]:
        for code in re.findall(r"\b[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}\b|\b\d{6}\b", part):
            if code.lower() in low:
                return True
    if not parts:
        return False
    street = parts[0]
    # Российский формат Google: "ул. Баумана, 44/8, Казань" — номер дома отдельной частью.
    if not re.search(r"\d", street) and len(parts) > 1:
        street = f"{street} {parts[1]}"
    houses = re.findall(r"\d+", street)[:1]
    words = [w for w in _words(street) if len(w) >= 4 and not w.isdigit() and w not in _GENERIC_STREET_WORDS]
    if not words:
        return False
    text_words = set(_words(text))
    if not any(w in text_words for w in words):
        return False
    return not houses or houses[0] in re.findall(r"\d+", low)


@dataclass
class OrgCard:
    """Что известно об организации из карточки Google Карт."""

    names: list[str]
    phone_digits: str | None = None
    address: str | None = None


@dataclass
class Match:
    name: bool = False
    phone: bool = False
    address: bool = False
    reasons: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.name and (self.phone or self.address)

    @property
    def score(self) -> int:
        return 2 * self.phone + self.address


def match(org: OrgCard, handle: str, display_name: str, profile_text: str) -> Match:
    result = Match(
        name=name_matches(org.names, handle, display_name),
        phone=phone_matches(org.phone_digits, profile_text),
        address=address_matches(org.address, profile_text),
    )
    result.reasons = [r for r, ok in (("название", result.name), ("телефон", result.phone), ("адрес", result.address)) if ok]
    return result
