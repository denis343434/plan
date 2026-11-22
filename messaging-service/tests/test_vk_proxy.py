from unittest.mock import MagicMock

import pytest

from app.adapters import vk
from app.config import settings


def test_no_proxy_by_default(monkeypatch):
    monkeypatch.setattr(settings, "VK_PROXY", "")
    pw = MagicMock()
    vk._launch_browser(pw)
    assert "proxy" not in pw.chromium.launch.call_args.kwargs


def test_proxy_credentials_passed_as_separate_fields(monkeypatch):
    monkeypatch.setattr(settings, "VK_PROXY", "http://user:p%40ss@10.0.0.1:3128")
    pw = MagicMock()
    vk._launch_browser(pw)
    assert pw.chromium.launch.call_args.kwargs["proxy"] == {
        "server": "http://10.0.0.1:3128",
        "username": "user",
        "password": "p@ss",
    }


def test_proxy_without_scheme_defaults_to_http(monkeypatch):
    monkeypatch.setattr(settings, "VK_PROXY", "10.0.0.1:3128")
    assert vk._proxy_settings() == {"server": "http://10.0.0.1:3128"}


def test_proxy_without_port_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "VK_PROXY", "http://user:pass@10.0.0.1")
    with pytest.raises(ValueError):
        vk._proxy_settings()
