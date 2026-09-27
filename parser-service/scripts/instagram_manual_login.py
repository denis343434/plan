"""Разовый ручной логин в Instagram (headful Playwright) для прогрева аккаунта парсера.

Открывает браузер, ждёт, пока человек залогинится вручную (включая возможную 2FA/чекпоинт),
затем сохраняет context.storage_state() и отправляет его в Data Service
(PUT /accounts/{account_id}/session) — тот же аккаунт нужно предварительно
создать через POST /accounts.

Дублирует parser-service/scripts/vk_manual_login.py — та же осознанная мелкая дупликация
вместо преждевременной абстракции, что и в messaging-service/scripts/vk_manual_login.py, см.
architecture.md.

Запуск:
    python scripts/instagram_manual_login.py --account-id <uuid> [--data-service-url http://localhost:8001]
"""

import argparse
import time

import httpx
from playwright.sync_api import sync_playwright


def _goto_with_retry(page, url: str, attempts: int = 3) -> None:
    """Открытие страницы логина Instagram изредка падает с транзитным сбоем сети/ответа
    (таймаут, ERR_HTTP_RESPONSE_CODE_FAILURE — подтверждено вживую) — без ретрая это роняет
    весь скрипт необработанным исключением, а вместе с ним и окно браузера/консоли мгновенно
    закрывается, прежде чем человек успевает что-то увидеть."""
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            page.goto(url)
            return
        except Exception as exc:
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(2.0)
    raise last_exc


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--data-service-url", default="http://localhost:8001")
    args = parser.parse_args()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        try:
            _goto_with_retry(page, "https://www.instagram.com/accounts/login/")
        except Exception as exc:
            # Не даём окну молча схлопнуться вместе с необработанным исключением — человек,
            # запустивший это из GUI (отдельная консоль), должен успеть увидеть, что пошло не
            # так, прежде чем окно закроется.
            print(f"Не удалось открыть страницу логина Instagram: {exc!r}")
            browser.close()
            input("Нажми Enter, чтобы закрыть это окно...")
            return

        input("Залогинься вручную (включая 2FA/чекпоинт, если есть), затем нажми Enter здесь...")

        storage_state = context.storage_state()
        browser.close()

    try:
        response = httpx.put(
            f"{args.data_service_url}/accounts/{args.account_id}/session",
            json={"storage_state": storage_state},
            timeout=10,
        )
        response.raise_for_status()
        print(f"Session saved for account {args.account_id}")
    except Exception as exc:
        print(f"Не удалось сохранить сессию в Data Service: {exc!r}")
        input("Нажми Enter, чтобы закрыть это окно...")


if __name__ == "__main__":
    main()
