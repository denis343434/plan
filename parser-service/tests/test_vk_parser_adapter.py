from unittest.mock import MagicMock

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from app.adapters.base import CaptchaDetectedError, ParseFilters
from app.adapters.vk import (
    _SEARCH_INPUT_SELECTOR,
    _SEARCH_RESULTS_LABEL,
    _SHOW_ALL_SELECTOR,
    SessionExpiredError,
    VkParserAdapter,
)
from app.config import settings


@pytest.fixture(autouse=True)
def _no_delay(monkeypatch):
    monkeypatch.setattr(settings, "MIN_DELAY_SEC", 0.0)
    monkeypatch.setattr(settings, "MAX_DELAY_SEC", 0.0)


def _make_page(
    captcha: bool = False,
    search_input_ok: bool = True,
    results_page_found: bool = True,
    show_all_present: bool = True,
) -> tuple[MagicMock, MagicMock, MagicMock | None]:
    page = MagicMock()
    search_input = MagicMock()
    show_all = MagicMock() if show_all_present else None

    def wait_for_selector(selector: str, timeout: int | None = None):
        if selector == _SEARCH_INPUT_SELECTOR:
            if not search_input_ok:
                raise PlaywrightTimeoutError("search input did not render")
            return search_input
        if selector == f"text={_SEARCH_RESULTS_LABEL}":
            if not results_page_found:
                raise PlaywrightTimeoutError("results page never appeared")
            return MagicMock()
        raise AssertionError(f"unexpected selector {selector}")

    def query_selector(selector: str):
        if selector == _SHOW_ALL_SELECTOR:
            return show_all
        return MagicMock() if captcha else None  # капча-селектор

    page.wait_for_selector.side_effect = wait_for_selector
    page.query_selector.side_effect = query_selector
    page.locator.return_value.count.return_value = 0  # экрана "войдите заново" нет
    return page, search_input, show_all


def test_open_communities_search_clicks_show_all_when_present():
    page, search_input, show_all = _make_page(show_all_present=True)

    adapter = VkParserAdapter(storage_state={})
    adapter._open_communities_search(page, "keyword", [])

    search_input.click.assert_called_once()
    search_input.type.assert_called_once()
    show_all.click.assert_called_once()


def test_open_communities_search_returns_when_results_page_never_loads():
    # Ложная подсказка-автокомплит появляется сразу, но настоящей страницы результатов
    # ("Результаты поиска") нет — не должны лезть за "Показать все" на недогруженную страницу.
    page, _, show_all = _make_page(results_page_found=False)

    adapter = VkParserAdapter(storage_state={})
    adapter._open_communities_search(page, "keyword", [])  # не должно бросать исключение
    show_all.click.assert_not_called()


def test_open_communities_search_returns_when_no_show_all_button():
    # Мало результатов — VK показывает все сообщества сразу на этой же странице, без кнопки.
    page, _, show_all = _make_page(show_all_present=False)

    adapter = VkParserAdapter(storage_state={})
    adapter._open_communities_search(page, "keyword", [])  # не должно бросать исключение
    assert show_all is None


def test_open_communities_search_raises_on_captcha():
    page, _, _ = _make_page(captcha=True)

    adapter = VkParserAdapter(storage_state={})
    with pytest.raises(CaptchaDetectedError):
        adapter._open_communities_search(page, "keyword", [])


def test_open_communities_search_raises_when_search_input_never_renders():
    page, _, _ = _make_page(search_input_ok=False)

    adapter = VkParserAdapter(storage_state={})
    with pytest.raises(RuntimeError):
        adapter._open_communities_search(page, "keyword", [])


def _make_site_check_page(goto_side_effect=None, header_ready=True, site_hosts=None, header_exc=None, evaluate_exc=None):
    page = MagicMock()
    if goto_side_effect is not None:
        page.goto.side_effect = goto_side_effect
    page.locator.return_value.count.return_value = 0  # экрана "войдите заново" нет

    if header_exc is not None:
        page.wait_for_function.side_effect = header_exc
    elif not header_ready:
        page.wait_for_function.side_effect = PlaywrightTimeoutError("group header never appeared")

    if evaluate_exc is not None:
        page.evaluate.side_effect = evaluate_exc
    else:
        page.evaluate.return_value = site_hosts or []
    return page


def test_has_external_site_true_when_site_field_has_domain():
    page = _make_site_check_page(site_hosts=["example.com"])

    adapter = VkParserAdapter(storage_state={})
    assert adapter._has_external_site(page, "https://vk.com/group") is True


def test_has_external_site_none_when_group_header_never_renders():
    # Шапка группы не отрисовалась за отведённое время — это НЕ "сайта нет": раньше здесь
    # возвращался False, и группа с сайтом на медленной странице попадала в "без сайта"
    # (запрос пользователя 2026-11-19). None = не смогли проверить, кандидат уйдёт на следующий прогон.
    page = _make_site_check_page(header_ready=False)

    adapter = VkParserAdapter(storage_state={})
    assert adapter._has_external_site(page, "https://vk.com/group") is None


def test_has_external_site_none_when_page_never_loads(monkeypatch):
    # Плохой интернет/сбой самого goto (не таймаут поля "сайт") — раньше попадал в ту же ветку,
    # что и "поля нет", и группа с реальным сайтом молча помечалась как без сайта (запрос
    # пользователя). None здесь означает "не удалось проверить", а не "сайта нет".
    monkeypatch.setattr("app.adapters.vk.time.sleep", lambda *_: None)
    page = _make_site_check_page(goto_side_effect=PlaywrightTimeoutError("goto timed out"))

    adapter = VkParserAdapter(storage_state={})
    assert adapter._has_external_site(page, "https://vk.com/group") is None
    assert page.goto.call_count == 2  # один ретрай перед тем, как сдаться


def test_has_external_site_retries_goto_and_succeeds_on_second_attempt(monkeypatch):
    monkeypatch.setattr("app.adapters.vk.time.sleep", lambda *_: None)
    page = _make_site_check_page(
        goto_side_effect=[Exception("network blip"), None],
        site_hosts=["example.com"],
    )

    adapter = VkParserAdapter(storage_state={})
    assert adapter._has_external_site(page, "https://vk.com/group") is True
    assert page.goto.call_count == 2


def test_has_external_site_none_when_site_field_check_fails_unexpectedly():
    page = _make_site_check_page(evaluate_exc=RuntimeError("boom"))

    adapter = VkParserAdapter(storage_state={})
    assert adapter._has_external_site(page, "https://vk.com/group") is None


def _make_anon_link(href: str, text: str = "Some Group"):
    link = MagicMock()
    link.get_attribute.return_value = href
    link.inner_text.return_value = text
    return link


def test_parse_anon_link_extracts_group_from_relative_and_absolute_href():
    adapter = VkParserAdapter(storage_state={})

    lead = adapter._parse_anon_link(_make_anon_link("/fitness_club1"))
    assert lead is not None
    assert lead.external_id == "fitness_club1"
    assert lead.group_url == "https://vk.com/fitness_club1"

    lead2 = adapter._parse_anon_link(_make_anon_link("https://vk.com/club12345?ref=search", text="Club"))
    assert lead2 is not None
    assert lead2.external_id == "club12345"


def test_parse_anon_link_filters_system_and_unrelated_links():
    adapter = VkParserAdapter(storage_state={})

    assert adapter._parse_anon_link(_make_anon_link("/search")) is None
    assert adapter._parse_anon_link(_make_anon_link("/settings")) is None
    assert adapter._parse_anon_link(_make_anon_link("https://vk.com/away.php?to=example.com")) is None
    assert adapter._parse_anon_link(_make_anon_link("javascript:void(0)")) is None


def test_parse_anon_link_skips_groups_with_excluded_title():
    adapter = VkParserAdapter(storage_state={})

    assert adapter._parse_anon_link(_make_anon_link("/models1", "Ищу модель")) is None
    assert adapter._parse_anon_link(_make_anon_link("/models2", "ИЩУ  МОДЕЛЬ на маникюр")) is None
    assert adapter._parse_anon_link(_make_anon_link("/salon", "Салон красоты")) is not None


def test_parse_card_skips_groups_with_excluded_title():
    adapter = VkParserAdapter(storage_state={})

    def make_card(href: str, title: str):
        link = MagicMock()
        link.get_attribute.side_effect = lambda name: href if name == "href" else title
        card = MagicMock()
        card.query_selector.return_value = link
        return card

    assert adapter._parse_card(make_card("/models", "Ищу модель | Москва"), ParseFilters()) is None
    lead = adapter._parse_card(make_card("/salon", "Салон красоты"), ParseFilters())
    assert lead is not None and lead.external_id == "salon"


def test_search_communities_anonymous_collects_and_dedupes_candidates():
    page = MagicMock()
    page.goto.return_value = None
    page.query_selector.return_value = None  # капча-селектор отсутствует
    page.query_selector_all.return_value = [
        _make_anon_link("/groupone", "Group One"),
        _make_anon_link("/groupone", "Group One"),  # дубль ссылки на ту же группу
        _make_anon_link("/known_group", "Known"),
        _make_anon_link("/search"),  # системная ссылка
    ]

    adapter = VkParserAdapter(storage_state={})
    filters = ParseFilters(known_external_ids=frozenset({"known_group"}))
    candidates = adapter._search_communities_anonymous(page, "fitness", filters, [])

    assert [c.external_id for c in candidates] == ["groupone"]


def test_search_communities_anonymous_raises_on_captcha():
    page = MagicMock()
    page.goto.return_value = None
    page.query_selector.return_value = MagicMock()  # капча-селектор найден
    page.query_selector_all.return_value = []

    adapter = VkParserAdapter(storage_state={})
    with pytest.raises(CaptchaDetectedError):
        adapter._search_communities_anonymous(page, "fitness", ParseFilters(), [])


def test_open_communities_search_raises_session_expired_when_login_required():
    # VK показывает экран "Выберите аккаунт для входа" вместо ленты — сохранённая сессия
    # протухла. Строка поиска в такой ситуации не появится за 30с (реальный экран входа —
    # не тот же компонент), поэтому проверка на "требуется вход" стоит именно после этого
    # таймаута, а не сразу после goto (см. запрос пользователя 2026-08-24: instant-проверка
    # сразу после goto ничего не находила — реальный контент VK иногда рисуется только через
    # 18-20с, см. комментарий в _open_communities_search).
    page, _, _ = _make_page(search_input_ok=False)

    def locator(selector):
        m = MagicMock()
        m.count.return_value = 1 if "Выберите аккаунт" in selector else 0
        return m

    page.locator.side_effect = locator

    adapter = VkParserAdapter(storage_state={})
    with pytest.raises(SessionExpiredError):
        adapter._open_communities_search(page, "keyword", [])


def test_has_external_site_reraises_dead_driver_instead_of_skipping():
    """Регрессия 2026-10-23: "Connection closed while reading from the driver" (умер node.exe
    Playwright) ловилась как сбой одного кандидата, цикл шёл дальше и навсегда зависал."""
    page = MagicMock()
    page.goto.side_effect = Exception("Page.goto: Connection closed while reading from the driver")

    with pytest.raises(Exception, match="Connection closed while reading from the driver"):
        VkParserAdapter(storage_state={})._has_external_site(page, "https://vk.com/club1")
    assert page.goto.call_count == 1  # без ретрая


def test_search_communities_dead_driver_raises_with_partial_leads(monkeypatch):
    from app.adapters import vk
    from app.adapters.base import BrowserCrashedError, RawLead

    pw = MagicMock()
    monkeypatch.setattr(vk, "sync_playwright", lambda: MagicMock(start=lambda: pw))
    context = pw.chromium.launch.return_value.new_context.return_value

    found = RawLead(external_id="club1", group_url="https://vk.com/club1", title=None)
    other = RawLead(external_id="club2", group_url="https://vk.com/club2", title=None)
    adapter = VkParserAdapter(storage_state={})
    monkeypatch.setattr(adapter, "_search_communities_anonymous", lambda *a: [found, other])
    calls = iter([False, Exception("Connection closed while reading from the driver")])

    def has_site(page, url):
        r = next(calls)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(adapter, "_has_external_site", has_site)

    with pytest.raises(BrowserCrashedError) as exc_info:
        adapter.search_communities("q", ParseFilters())
    assert exc_info.value.partial_leads == [found]
    # драйвер мёртв — закрытие пропускаем, иначе оно может повесить поток
    context.close.assert_not_called()
    pw.stop.assert_not_called()
