"""Разовый ручной вход в Telegram Web / WhatsApp Web / Facebook (headful Playwright) для аккаунта.

Открывает браузер, ждёт, пока человек войдёт (QR-код с телефона или номер + код), затем
сохраняет context.storage_state(indexed_db=True) и отправляет его в Data Service
(PUT /accounts/{account_id}/session). IndexedDB обязателен: WhatsApp Web хранит авторизацию
именно там, а не в cookies/localStorage (нужен Playwright >= 1.51).

Тот же принцип, что у vk_manual_login.py/instagram_manual_login.py (см. architecture.md про
осознанную мелкую дупликацию скриптов логина).

Для Telegram вход идёт через тот же прокси, что и рассылка (TG_PROXY) — у аккаунта не должен
меняться IP между входом и отправкой. Прокси берётся из --proxy, иначе из переменной окружения
TG_PROXY, иначе из messaging-service/.env (оттуда же его читает сама рассылка).

Запуск:
    python scripts/messenger_manual_login.py --platform tg|whatsapp|facebook --account-id <uuid> [--proxy http://login:pass@host:port] [--data-service-url http://localhost:8001]
"""

import argparse
import os
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx
from playwright.sync_api import sync_playwright

LOGIN_URLS = {
    "tg": "https://web.telegram.org/k/",
    "whatsapp": "https://web.whatsapp.com/",
    "facebook": "https://www.facebook.com/login/",
}
TITLES = {"tg": "Telegram", "whatsapp": "WhatsApp", "facebook": "Facebook"}
# Что сделать человеку в открывшемся окне перед нажатием Enter.
LOGIN_HINTS = {
    "tg": "отсканируй QR-код телефоном или войди по номеру, дождись списка чатов",
    "whatsapp": "отсканируй QR-код телефоном, дождись списка чатов",
    "facebook": "введи логин и пароль (и код 2FA/подтверждение, если попросит), дождись ленты",
}
# Тот же UA, что у messaging-service/app/adapters/web_messenger.py — сессия, сохранённая под
# одним fingerprint, а используемая под другим, чаще вылетает.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
MESSAGING_ENV = Path(__file__).resolve().parents[2] / "messaging-service" / ".env"


def _env_value(name: str) -> str:
    if os.environ.get(name):
        return os.environ[name]
    if MESSAGING_ENV.exists():
        for line in MESSAGING_ENV.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == name:
                return value.strip().strip('"').strip("'")
    return ""


def _proxy_from_env(platform: str) -> str:
    # Как в messaging-service (TelegramSendAdapter/FacebookSendAdapter.proxy): у Facebook свой
    # FB_PROXY, пустой — TG_PROXY. Вход и рассылка должны идти с одного IP.
    if platform == "facebook":
        return _env_value("FB_PROXY") or _env_value("TG_PROXY")
    return _env_value("TG_PROXY")


def _proxy_settings(raw: str) -> dict | None:
    # Копия messaging-service/app/adapters/proxy.py::parse_proxy_url — скрипт запускается
    # отдельно от сервиса, своим интерпретатором, и не может импортировать его код.
    raw = raw.strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = f"http://{raw}"
    parts = urlsplit(raw)
    if not parts.hostname or not parts.port:
        raise ValueError(f"прокси должен выглядеть как http://login:pass@host:port, получено {parts.hostname!r}")
    proxy = {"server": f"{parts.scheme}://{parts.hostname}:{parts.port}"}
    if parts.username:
        proxy["username"] = unquote(parts.username)
    if parts.password:
        proxy["password"] = unquote(parts.password)
    return proxy


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, choices=sorted(LOGIN_URLS))
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--data-service-url", default="http://localhost:8001")
    parser.add_argument("--proxy", default=None, help="для tg и facebook; по умолчанию TG_PROXY / FB_PROXY")
    args = parser.parse_args()
    title = TITLES[args.platform]

    launch_kwargs = {"headless": False, "args": ["--disable-blink-features=AutomationControlled"]}
    if args.platform in ("tg", "facebook"):
        try:
            proxy = _proxy_settings(args.proxy if args.proxy is not None else _proxy_from_env(args.platform))
        except ValueError as exc:
            print(f"Неверный прокси: {exc}")
            input("Нажми Enter, чтобы закрыть это окно...")
            return
        if proxy is not None:
            print(f"{title} откроется через прокси {proxy['server']}")
            launch_kwargs["proxy"] = proxy

    with sync_playwright() as pw:
        browser = pw.chromium.launch(**launch_kwargs)
        context = browser.new_context(user_agent=USER_AGENT, locale="ru-RU", viewport={"width": 1280, "height": 860})
        page = context.new_page()
        try:
            page.goto(LOGIN_URLS[args.platform])
        except Exception as exc:
            print(f"Не удалось открыть {title}: {exc!r}")
            browser.close()
            input("Нажми Enter, чтобы закрыть это окно...")
            return

        input(
            f"Войди в {title} ({LOGIN_HINTS[args.platform]}), затем нажми Enter здесь..."
        )

        storage_state = context.storage_state(indexed_db=True)
        browser.close()

    try:
        response = httpx.put(
            f"{args.data_service_url}/accounts/{args.account_id}/session",
            json={"storage_state": storage_state},
            timeout=60,
        )
        response.raise_for_status()
        print(f"Session saved for account {args.account_id}")
    except Exception as exc:
        print(f"Не удалось сохранить сессию в Data Service: {exc!r}")
        input("Нажми Enter, чтобы закрыть это окно...")


if __name__ == "__main__":
    main()
