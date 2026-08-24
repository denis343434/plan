from unittest.mock import MagicMock

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from app.adapters.base import CaptchaDetectedError
from app.adapters.vk import (
    _COMMUNITIES_SECTION_LABEL,
    _SEARCH_INPUT_SELECTOR,
    _SEARCH_RESULTS_LABEL,
    SessionExpiredError,
    VkParserAdapter,
)
from app.config import settings


@pytest.fixture(autouse=True)
def _no_delay(monkeypatch):
    monkeypatch.setattr(settings, "MIN_DELAY_SEC", 0.0)
    monkeypatch.setattr(settings, "MAX_DELAY_SEC", 0.0)


def _make_header(text: str, show_all_present: bool) -> tuple[MagicMock, MagicMock | None]:
    header = MagicMock()
    header.inner_text.return_value = text
    show_all = MagicMock() if show_all_present else None
    header.query_selector.return_value = show_all
    return header, show_all


def _make_page(
    headers: list[MagicMock],
    captcha: bool = False,
    search_input_ok: bool = True,
    results_page_found: bool = True,
    communities_section_found: bool = True,
) -> tuple[MagicMock, MagicMock]:
    page = MagicMock()
    search_input = MagicMock()

    def wait_for_selector(selector: str, timeout: int | None = None):
        if selector == _SEARCH_INPUT_SELECTOR:
            if not search_input_ok:
                raise PlaywrightTimeoutError("search input did not render")
            return search_input
        raise AssertionError(f"unexpected selector {selector}")

    def wait_for_function(script: str, arg=None, timeout: int | None = None):
        if arg == _SEARCH_RESULTS_LABEL:
            if not results_page_found:
                raise PlaywrightTimeoutError("results page never appeared")
            return None
        if arg == _COMMUNITIES_SECTION_LABEL:
            if not communities_section_found:
                raise PlaywrightTimeoutError('"Сообщества" section never appeared')
            return None
        raise AssertionError(f"unexpected wait_for_function arg {arg!r}")

    page.wait_for_selector.side_effect = wait_for_selector
    page.wait_for_function.side_effect = wait_for_function
    page.query_selector_all.return_value = headers
    page.query_selector.return_value = MagicMock() if captcha else None  # капча-селектор
    page.locator.return_value.count.return_value = 0  # экрана "войдите заново" нет
    return page, search_input


def test_open_communities_search_clicks_show_all_when_present():
    header, show_all = _make_header("Сообщества\nПоказать все", show_all_present=True)
    page, search_input = _make_page([header])

    adapter = VkParserAdapter(storage_state={})
    adapter._open_communities_search(page, "keyword", [])

    search_input.click.assert_called_once()
    search_input.type.assert_called_once()
    show_all.click.assert_called_once()


def test_open_communities_search_returns_when_results_page_never_loads():
    # Ложная подсказка-автокомплит ("Недавние"/"Люди"/"Сообщества") появляется сразу, но
    # настоящая страница результатов ("Результаты поиска") — уже нет. Не должны провалиться
    # сквозь неё и искать секцию "Сообщества" в ещё не загруженной странице.
    page, _ = _make_page([], results_page_found=False)

    adapter = VkParserAdapter(storage_state={})
    adapter._open_communities_search(page, "keyword", [])  # не должно бросать исключение
    page.query_selector_all.assert_not_called()


def test_open_communities_search_returns_when_communities_section_never_appears():
    page, _ = _make_page([], communities_section_found=False)

    adapter = VkParserAdapter(storage_state={})
    adapter._open_communities_search(page, "keyword", [])  # не должно бросать исключение
    page.query_selector_all.assert_not_called()


def test_open_communities_search_returns_when_no_show_all_button():
    # Мало результатов — VK показывает все сообщества сразу на этой же странице, без кнопки.
    header, _ = _make_header("Сообщества", show_all_present=False)
    page, _ = _make_page([header])

    adapter = VkParserAdapter(storage_state={})
    adapter._open_communities_search(page, "keyword", [])  # не должно бросать исключение


def test_open_communities_search_raises_on_captcha():
    page, _ = _make_page([], captcha=True)

    adapter = VkParserAdapter(storage_state={})
    with pytest.raises(CaptchaDetectedError):
        adapter._open_communities_search(page, "keyword", [])


def test_open_communities_search_raises_when_search_input_never_renders():
    page, _ = _make_page([], search_input_ok=False)

    adapter = VkParserAdapter(storage_state={})
    with pytest.raises(RuntimeError):
        adapter._open_communities_search(page, "keyword", [])


def test_open_communities_search_raises_session_expired_when_login_required():
    # VK показывает экран "Выберите аккаунт для входа" вместо ленты — сохранённая сессия
    # протухла. Строка поиска в такой ситуации не появится за 30с (реальный экран входа —
    # не тот же компонент), поэтому проверка на "требуется вход" стоит именно после этого
    # таймаута, а не сразу после goto (см. запрос пользователя 2026-08-24: instant-проверка
    # сразу после goto ничего не находила — реальный контент VK иногда рисуется только через
    # 18-20с, см. комментарий в _open_communities_search).
    page, _ = _make_page([], search_input_ok=False)
    page.locator.return_value.count.return_value = 1

    adapter = VkParserAdapter(storage_state={})
    with pytest.raises(SessionExpiredError):
        adapter._open_communities_search(page, "keyword", [])
