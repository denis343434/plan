from app.adapters import registry
from app.adapters.dryrun import DryRunAdapter
from app.adapters.web_messenger import FacebookSendAdapter, TelegramSendAdapter, WhatsAppSendAdapter
from app.config import settings


def test_telegram_chat_url_by_username_and_phone():
    adapter = TelegramSendAdapter({})
    assert adapter.chat_url({"external_id": "anatomy_kazan"}) == "https://web.telegram.org/k/#@anatomy_kazan"
    assert adapter.chat_url({"external_id": "+79991234567"}) == (
        "https://web.telegram.org/k/#?tgaddr=tg%3A%2F%2Fresolve%3Fphone%3D79991234567"
    )
    assert adapter.chat_url({"external_id": ""}) is None


def test_whatsapp_chat_url_from_phone():
    adapter = WhatsAppSendAdapter({})
    assert adapter.chat_url({"external_id": "79931345133"}) == "https://web.whatsapp.com/send?phone=79931345133"


def test_registry_picks_messenger_adapters_only_in_playwright_mode(monkeypatch):
    monkeypatch.setattr(settings, "TG_ADAPTER_MODE", "fake")
    monkeypatch.setattr(settings, "WA_ADAPTER_MODE", "fake")
    assert isinstance(registry.get_adapter("tg"), DryRunAdapter)
    assert isinstance(registry.get_adapter("whatsapp"), DryRunAdapter)

    monkeypatch.setattr(settings, "TG_ADAPTER_MODE", "playwright")
    monkeypatch.setattr(settings, "WA_ADAPTER_MODE", "playwright")
    assert isinstance(registry.get_adapter("tg"), TelegramSendAdapter)
    assert isinstance(registry.get_adapter("whatsapp"), WhatsAppSendAdapter)


def test_telegram_browser_uses_tg_proxy(monkeypatch):
    from unittest.mock import MagicMock

    monkeypatch.setattr(settings, "TG_PROXY", "http://user:p%40ss@10.0.0.2:8080")
    pw = MagicMock()
    TelegramSendAdapter({})._launch(pw)
    assert pw.chromium.launch.call_args.kwargs["proxy"] == {
        "server": "http://10.0.0.2:8080",
        "username": "user",
        "password": "p@ss",
    }


def test_whatsapp_browser_ignores_tg_proxy(monkeypatch):
    from unittest.mock import MagicMock

    monkeypatch.setattr(settings, "TG_PROXY", "http://10.0.0.2:8080")
    pw = MagicMock()
    WhatsAppSendAdapter({})._launch(pw)
    assert "proxy" not in pw.chromium.launch.call_args.kwargs


def test_facebook_chat_url_and_registry(monkeypatch):
    adapter = FacebookSendAdapter({})
    assert adapter.chat_url({"external_id": "coffee.kzn"}) == "https://www.facebook.com/messages/t/coffee.kzn"
    assert adapter.chat_url({"external_id": "100064"}) == "https://www.facebook.com/messages/t/100064"
    assert adapter.chat_url({"external_id": ""}) is None

    monkeypatch.setattr(settings, "FB_ADAPTER_MODE", "fake")
    assert isinstance(registry.get_adapter("facebook"), DryRunAdapter)
    monkeypatch.setattr(settings, "FB_ADAPTER_MODE", "playwright")
    assert isinstance(registry.get_adapter("facebook"), FacebookSendAdapter)


def test_facebook_proxy_falls_back_to_tg_proxy(monkeypatch):
    monkeypatch.setattr(settings, "TG_PROXY", "http://10.0.0.2:8080")
    monkeypatch.setattr(settings, "FB_PROXY", "")
    assert FacebookSendAdapter({}).proxy() == {"server": "http://10.0.0.2:8080"}
    monkeypatch.setattr(settings, "FB_PROXY", "http://10.0.0.3:3128")
    assert FacebookSendAdapter({}).proxy() == {"server": "http://10.0.0.3:3128"}
