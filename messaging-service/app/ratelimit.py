import random
import re
import time
from typing import Callable

from app.config import settings


def delay(min_sec: float, max_sec: float) -> None:
    time.sleep(random.uniform(min_sec, max_sec))


def _platform_range(platform: str) -> tuple[float, float]:
    if platform == "instagram":
        return settings.IG_MIN_DELAY_SEC, settings.IG_MAX_DELAY_SEC
    if platform == "tg":
        return settings.TG_MIN_DELAY_SEC, settings.TG_MAX_DELAY_SEC
    if platform == "whatsapp":
        return settings.WA_MIN_DELAY_SEC, settings.WA_MAX_DELAY_SEC
    if platform == "facebook":
        return settings.FB_MIN_DELAY_SEC, settings.FB_MAX_DELAY_SEC
    return settings.MIN_DELAY_SEC, settings.MAX_DELAY_SEC


def _sleep(seconds: float, should_stop: Callable[[], bool] | None = None) -> None:
    """time.sleep, который можно прервать кнопкой «Стоп» (длинную паузу рвём на куски)."""
    if seconds <= 0:
        return
    if should_stop is None:
        time.sleep(seconds)
        return

    deadline = time.monotonic() + seconds
    while True:
        if should_stop():
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.5, remaining))


def delay_for_platform(
    platform: str,
    should_stop: Callable[[], bool] | None = None,
    elapsed_sec: float | None = None,
) -> None:
    """Пауза между лидами. Instagram банит агрессивнее VK — свой, более консервативный диапазон.

    elapsed_sec — сколько текущий цикл уже длится (получение аккаунта + отправка + запись в БД).
    Для WhatsApp при заданном WA_CYCLE_MIN_SEC пауза добирает цикл до случайной цели из
    [WA_CYCLE_MIN_SEC, WA_CYCLE_MAX_SEC] (по умолчанию 110-125с), а не добавляет фиксированные
    85-95с сверху. elapsed_sec=None (ручной ответ в reply.py) оставляет прежнее поведение с
    WA_MIN/MAX_DELAY_SEC.

    should_stop — проверка кнопки «Стоп»: длинную паузу (например, WhatsApp 40 сообщений/час =
    ~90 секунд) рвём на куски, чтобы остановка не ждала её целиком.
    """
    if platform == "whatsapp" and settings.WA_CYCLE_MIN_SEC > 0 and elapsed_sec is not None:
        lo = settings.WA_CYCLE_MIN_SEC
        hi = max(lo, settings.WA_CYCLE_MAX_SEC)
        target = random.uniform(lo, hi)
        _sleep(max(0.0, target - elapsed_sec), should_stop)
        return

    lo, hi = _platform_range(platform)
    if should_stop is None:
        delay(lo, hi)
        return
    _sleep(random.uniform(lo, hi), should_stop)


def settle() -> None:
    """Короткая пауза "дать странице устояться" внутри send_message — не rate-limit."""
    delay(settings.SEND_SETTLE_MIN_SEC, settings.SEND_SETTLE_MAX_SEC)


def type_chunks(text: str) -> list[str]:
    """Режет текст на слова вместе с хвостовыми пробелами — набор идёт по словам (один вызов
    Playwright на слово), а не по символу (один вызов на символ)."""
    return re.findall(r"\S+\s*|\s+", text)


def typing_delay_ms() -> float:
    return random.uniform(settings.TYPING_DELAY_MIN_MS, settings.TYPING_DELAY_MAX_MS)
