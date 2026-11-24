import os
import time
from types import SimpleNamespace

import pytest

from app.adapters.registry import get_adapter
from app.adapters.whatsapp_desktop import WhatsAppDesktopAdapter, _UiItem, _digits, _normalize
from app.config import settings

pytestmark = pytest.mark.skipif(os.name != "nt", reason="WhatsApp Desktop adapter is Windows-only")


def test_registry_selects_desktop_whatsapp_adapter(monkeypatch):
    monkeypatch.setattr(settings, "WA_ADAPTER_MODE", "desktop")
    assert isinstance(get_adapter("whatsapp"), WhatsAppDesktopAdapter)


def test_desktop_adapter_helpers():
    assert _digits("+7 (951) 867-70-86") == "79518677086"
    assert _normalize("  Лёха  ") == "леха"
    assert _normalize("ЛЕХА") == "леха"


def test_desktop_adapter_rejects_lead_without_number():
    result = WhatsAppDesktopAdapter().send_message({"external_id": ""}, {}, "hi")
    assert not result.success
    assert "номер" in (result.error or "")


def test_desktop_adapter_does_not_support_images():
    result = WhatsAppDesktopAdapter().send_message(
        {"external_id": "79518677086"}, {}, "hi", image={"name": "x.png"}
    )
    assert not result.success
    assert "изображен" in (result.error or "")


def _item(text: str, left: int = 0, top: int = 0) -> _UiItem:
    return _UiItem(
        text=text,
        element=SimpleNamespace(),
        _rect=SimpleNamespace(left=left, top=top),
        _rect_ready=True,
    )


class _ScriptedAdapter(WhatsAppDesktopAdapter):
    """Подменяет чтение UIA заранее заданными снимками окна — проверяем логику ожидания без
    реального WhatsApp Desktop."""

    def __init__(self, snapshots: list[list[_UiItem]]) -> None:
        super().__init__()
        self._snapshots = snapshots
        self.reads = 0

    def _read_ui(self, window):
        index = min(self.reads, len(self._snapshots) - 1)
        self.reads += 1
        return self._snapshots[index]


def _window():
    rect = SimpleNamespace(left=0, top=0, width=lambda: 1400, height=lambda: 850)
    return SimpleNamespace(rectangle=lambda: rect)


def test_wait_for_chat_stops_as_soon_as_right_chat_is_open(monkeypatch):
    monkeypatch.setattr(settings, "WA_DESKTOP_CHAT_DELAY_SEC", 5.0)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    adapter = _ScriptedAdapter(
        [
            [_item("Список чатов")],
            [_item("Данные контакта +7 951 867-70-86", left=500, top=20)],
        ]
    )

    items, rect, state = adapter._wait_for_chat(_window(), "79518677086", "Кафе Миндаль")

    assert state is None
    assert adapter.reads == 2  # не ждём весь таймаут после появления шапки
    assert adapter._header_matches(adapter._chat_header(items, rect), "79518677086", "Кафе Миндаль")


def test_wait_for_chat_returns_invalid_number_immediately(monkeypatch):
    monkeypatch.setattr(settings, "WA_DESKTOP_CHAT_DELAY_SEC", 5.0)
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda sec: sleeps.append(sec))
    adapter = _ScriptedAdapter([[_item("Номер не зарегистрирован в WhatsApp")]])

    _, _, state = adapter._wait_for_chat(_window(), "79518677086", None)

    assert state == "invalid"
    assert adapter.reads == 1
    assert sleeps == [0.4]  # только стартовая пауза, остаток таймаута не выжидаем


def test_wait_for_chat_closes_contact_info_panel(monkeypatch):
    monkeypatch.setattr(settings, "WA_DESKTOP_CHAT_DELAY_SEC", 5.0)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    adapter = _ScriptedAdapter(
        [
            [
                _item("Данные контакта", left=900, top=20),
                _item("Данные контакта +7 951 867-70-86", left=500, top=20),
            ],
            [_item("Данные контакта +7 951 867-70-86", left=500, top=20)],
        ]
    )
    escapes: list[int] = []
    monkeypatch.setattr(adapter, "_press_escape", lambda: escapes.append(1))

    items, rect, state = adapter._wait_for_chat(_window(), "79518677086", None)

    assert escapes == [1]
    assert adapter.reads == 2
    assert state is None
    assert adapter._header_matches(adapter._chat_header(items, rect), "79518677086", None)


def test_get_window_is_cached_between_messages(monkeypatch):
    adapter = WhatsAppDesktopAdapter()
    window = SimpleNamespace(
        window_text=lambda: "WhatsApp",
        is_minimized=lambda: False,
        set_focus=lambda: None,
    )
    finds: list[int] = []
    monkeypatch.setattr(adapter, "_wait_for_window", lambda: finds.append(1) or window)
    monkeypatch.setattr(time, "sleep", lambda _: None)

    assert adapter._get_window() is window
    assert adapter._get_window() is window
    assert len(finds) == 1


def test_get_window_refinds_after_window_closed(monkeypatch):
    adapter = WhatsAppDesktopAdapter()
    first = SimpleNamespace(
        window_text=lambda: "WhatsApp",
        is_minimized=lambda: False,
        set_focus=lambda: None,
    )
    second = SimpleNamespace(
        window_text=lambda: "WhatsApp",
        is_minimized=lambda: False,
        set_focus=lambda: None,
    )
    adapter._window = first

    def _closed():
        raise RuntimeError("element not found")

    first.window_text = _closed
    monkeypatch.setattr(adapter, "_wait_for_window", lambda: second)
    monkeypatch.setattr(time, "sleep", lambda _: None)

    assert adapter._get_window() is second
