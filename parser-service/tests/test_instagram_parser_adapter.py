from unittest.mock import MagicMock

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from app.adapters.base import CaptchaDetectedError
from app.adapters.instagram import InstagramParserAdapter, SessionExpiredError
from app.config import settings


@pytest.fixture(autouse=True)
def _no_delay(monkeypatch):
    monkeypatch.setattr(settings, "IG_MIN_DELAY_SEC", 0.0)
    monkeypatch.setattr(settings, "IG_MAX_DELAY_SEC", 0.0)


def _make_item(href: str, text: str = "some_user\nSome User") -> MagicMock:
    item = MagicMock()
    item.get_attribute.return_value = href
    item.inner_text.return_value = text
    return item


def test_parse_suggestion_returns_lead_for_profile_link():
    adapter = InstagramParserAdapter(storage_state={})
    lead = adapter._parse_suggestion(_make_item("/some_user/"))
    assert lead is not None
    assert lead.external_id == "some_user"
    assert lead.group_url == "https://www.instagram.com/some_user/"
    assert lead.title == "some_user"


def test_parse_suggestion_skips_hashtag_link():
    adapter = InstagramParserAdapter(storage_state={})
    assert adapter._parse_suggestion(_make_item("/explore/tags/fitness/")) is None


def test_parse_suggestion_skips_non_root_path():
    adapter = InstagramParserAdapter(storage_state={})
    assert adapter._parse_suggestion(_make_item("/some_user/reels/")) is None


def _make_bio_page(goto_side_effect=None, header_timeout=False, external_href=None):
    page = MagicMock()
    if goto_side_effect is not None:
        page.goto.side_effect = goto_side_effect
    page.inner_text.return_value = ""  # без capcha/login-wall маркеров

    if header_timeout:
        page.wait_for_selector.side_effect = PlaywrightTimeoutError("header never appeared")
        page.query_selector.return_value = None
        return page

    page.wait_for_selector.return_value = MagicMock()
    header = MagicMock()
    link = MagicMock()
    link.get_attribute.return_value = external_href
    header.query_selector_all.return_value = [link] if external_href else []
    page.query_selector.return_value = header
    return page


def test_has_bio_link_true_when_external_link_present():
    page = _make_bio_page(external_href="https://example.com")
    adapter = InstagramParserAdapter(storage_state={})
    assert adapter._has_bio_link(page, "https://www.instagram.com/some_user/") is True


def test_has_bio_link_false_when_only_instagram_links_present():
    page = _make_bio_page(external_href="https://www.instagram.com/other/")
    adapter = InstagramParserAdapter(storage_state={})
    assert adapter._has_bio_link(page, "https://www.instagram.com/some_user/") is False


def test_has_bio_link_false_when_header_never_appears():
    page = _make_bio_page(header_timeout=True)
    adapter = InstagramParserAdapter(storage_state={})
    assert adapter._has_bio_link(page, "https://www.instagram.com/some_user/") is False


def test_has_bio_link_none_when_page_never_loads(monkeypatch):
    monkeypatch.setattr("app.adapters.instagram.time.sleep", lambda *_: None)
    page = _make_bio_page(goto_side_effect=PlaywrightTimeoutError("goto timed out"))
    adapter = InstagramParserAdapter(storage_state={})
    assert adapter._has_bio_link(page, "https://www.instagram.com/some_user/") is None
    assert page.goto.call_count == 2


def test_raise_if_captcha_detects_checkpoint_marker():
    page = MagicMock()
    page.inner_text.return_value = "Help us confirm it's you"
    adapter = InstagramParserAdapter(storage_state={})
    with pytest.raises(CaptchaDetectedError):
        adapter._raise_if_captcha(page, [])


def test_raise_if_login_required_detects_login_wall():
    from app.adapters.instagram import _raise_if_login_required

    page = MagicMock()
    page.inner_text.return_value = "Log in to continue"
    with pytest.raises(SessionExpiredError):
        _raise_if_login_required(page)
