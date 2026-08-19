"""Разовый ручной логин в VK (headful Playwright) для прогрева аккаунта отправки.

Открывает браузер, ждёт, пока человек залогинится вручную (включая возможную 2FA),
затем сохраняет context.storage_state() и отправляет его в Data Service
(PUT /accounts/{account_id}/session) — тот же аккаунт нужно предварительно
создать через POST /accounts.

Дублирует parser-service/scripts/vk_manual_login.py — осознанная мелкая дупликация
вместо преждевременной абстракции на MVP-масштабе, см. architecture.md.

Запуск:
    python scripts/vk_manual_login.py --account-id <uuid> [--data-service-url http://localhost:8001]
"""

import argparse

import httpx
from playwright.sync_api import sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--data-service-url", default="http://localhost:8001")
    args = parser.parse_args()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto("https://vk.com/login")

        input("Залогинься вручную (включая 2FA, если есть), затем нажми Enter здесь...")

        storage_state = context.storage_state()
        browser.close()

    response = httpx.put(
        f"{args.data_service_url}/accounts/{args.account_id}/session",
        json={"storage_state": storage_state},
        timeout=10,
    )
    response.raise_for_status()
    print(f"Session saved for account {args.account_id}")


if __name__ == "__main__":
    main()
