import random
import re
import time

from app.config import settings


def delay(min_sec: float, max_sec: float) -> None:
    time.sleep(random.uniform(min_sec, max_sec))


def delay_for_platform(platform: str) -> None:
    """Пауза между лидами. Instagram банит агрессивнее VK — свой, более консервативный диапазон
    (раньше IG-адаптер добирал его тремя паузами внутри send_message, см. config.py)."""
    if platform == "instagram":
        delay(settings.IG_MIN_DELAY_SEC, settings.IG_MAX_DELAY_SEC)
    elif platform == "tg":
        delay(settings.TG_MIN_DELAY_SEC, settings.TG_MAX_DELAY_SEC)
    elif platform == "whatsapp":
        delay(settings.WA_MIN_DELAY_SEC, settings.WA_MAX_DELAY_SEC)
    elif platform == "facebook":
        delay(settings.FB_MIN_DELAY_SEC, settings.FB_MAX_DELAY_SEC)
    else:
        delay(settings.MIN_DELAY_SEC, settings.MAX_DELAY_SEC)


def settle() -> None:
    """Короткая пауза "дать странице устояться" внутри send_message — не rate-limit."""
    delay(settings.SEND_SETTLE_MIN_SEC, settings.SEND_SETTLE_MAX_SEC)


def type_chunks(text: str) -> list[str]:
    """Режет текст на слова вместе с хвостовыми пробелами — набор идёт по словам (один вызов
    Playwright на слово), а не по символу (один вызов на символ)."""
    return re.findall(r"\S+\s*|\s+", text)


def typing_delay_ms() -> float:
    return random.uniform(settings.TYPING_DELAY_MIN_MS, settings.TYPING_DELAY_MAX_MS)
