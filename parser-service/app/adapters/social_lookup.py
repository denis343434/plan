"""Поиск организации из Google Карт в Instagram / Facebook, когда в карточке нет ссылки.

Ищем по названию (+ город), каждого кандидата открываем и сверяем с карточкой
(social_match.match: название + телефон или адрес). Работает от аккаунтов из пула (их сессии
передаёт tasks.py) в том же браузере, что и парсер карт, — каждой соцсети свой контекст.

Проверено вживую 2026-10-04 на "Naru, 230 Shaftesbury Ave, London":
  - Instagram: /web/search/topsearch/?query=… (JSON, с cookies сессии и x-ig-app-id) — работает;
    /api/v1/users/web_profile_info отвечает 429, поэтому профиль читаем со страницы — в <header>
    есть имя, био (там телефон "02073797962") и адрес;
  - Facebook: /search/pages/?q=… — ссылки на страницы в div[role=main]; сама страница — текстом.
Никаких действий в соцсетях (подписки, лайки) — только поиск и просмотр.
"""

import logging
import random
import time
from urllib.parse import quote

from playwright.sync_api import Browser, Page

from app.adapters import contacts
from app.adapters.base import RawLead
from app.adapters.social_match import Match, OrgCard, match, name_matches
from app.config import settings

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
_IG_APP_ID = "936619743392459"  # публичный id веб-клиента Instagram, без него topsearch отвечает 400
_IG_SEARCH_URL = "https://www.instagram.com/web/search/topsearch/?context=blended&query={query}"
_FB_SEARCH_URL = "https://www.facebook.com/search/pages/?q={query}"
_LOGIN_MARKERS = ("log in to continue", "войдите, чтобы продолжить", "log into facebook", "вход на facebook")
# Сколько похожих по названию кандидатов открываем на одну соцсеть — каждый это загрузка страницы.
_MAX_CANDIDATES = 4
_PAGE_SETTLE_MS = 3500
_PROFILE_TIMEOUT_MS = 15_000


class SocialLookup:
    def __init__(self, browser: Browser, sessions: dict[str, dict]) -> None:
        self._browser = browser
        self._sessions = sessions
        self._pages: dict[str, Page] = {}
        # Соцсеть, где сессия протухла или нас заблокировали, до конца прогона больше не трогаем.
        self._disabled: set[str] = set()

    def available(self, channel: str) -> bool:
        return channel in self._sessions and channel not in self._disabled

    def find(self, channel: str, org: OrgCard, queries: list[str]) -> tuple[RawLead, Match] | None:
        if not self.available(channel):
            return None
        try:
            if channel == "instagram":
                return self._find_instagram(org, queries)
            if channel == "facebook":
                return self._find_facebook(org, queries)
        except Exception:  # noqa: BLE001 - поиск в соцсети не должен ронять парсинг карт
            logger.warning("social lookup: %s search failed for %s", channel, org.names, exc_info=True)
        return None

    def close(self) -> None:
        for page in self._pages.values():
            try:
                page.context.close()
            except Exception:  # noqa: BLE001
                logger.debug("failed to close lookup context", exc_info=True)
        self._pages.clear()

    # ---------- Instagram ----------
    def _find_instagram(self, org: OrgCard, queries: list[str]) -> tuple[RawLead, Match] | None:
        page = self._page("instagram", "https://www.instagram.com/")
        if page is None:
            return None
        candidates: dict[str, str] = {}
        for query in queries:
            resp = page.request.get(
                _IG_SEARCH_URL.format(query=quote(query)),
                headers={"x-ig-app-id": _IG_APP_ID, "x-requested-with": "XMLHttpRequest", "referer": "https://www.instagram.com/"},
            )
            if resp.status in (401, 403):
                self._disable("instagram", f"search answered {resp.status}")
                return None
            if not resp.ok:
                logger.warning("social lookup: instagram search %r answered %s", query, resp.status)
                continue
            for item in (resp.json().get("users") or [])[:10]:
                user = item.get("user") or {}
                username, full_name = user.get("username") or "", user.get("full_name") or ""
                if username and name_matches(org.names, username, full_name):
                    candidates.setdefault(username, full_name)
            _pause()
            if candidates:
                break  # запросы идут от точного к общему — дальше только шум
        best = None
        for username, full_name in list(candidates.items())[:_MAX_CANDIDATES]:
            page.goto(f"https://www.instagram.com/{username}/", wait_until="domcontentloaded")
            # Шапка профиля дорисовывается через 3-7 с после загрузки (проверено вживую).
            try:
                page.wait_for_selector("header", timeout=_PROFILE_TIMEOUT_MS)
                page.wait_for_timeout(500)
            except Exception:  # noqa: BLE001 - нет шапки — проверим логин-стену ниже и сверим что есть
                logger.debug("instagram profile header did not render for @%s", username)
            text = self._text(page, "header") or ""
            if self._login_wall(page, "instagram"):
                return None
            result = match(org, username, full_name, text)
            logger.info("social lookup: instagram @%s → %s", username, result.reasons or "нет совпадений")
            if result.ok and (best is None or result.score > best[1].score):
                best = (contacts.instagram_lead_from_username(username), result)
            _pause()
        return best

    # ---------- Facebook ----------
    def _find_facebook(self, org: OrgCard, queries: list[str]) -> tuple[RawLead, Match] | None:
        page = self._page("facebook", None)
        if page is None:
            return None
        candidates: dict[str, tuple[RawLead, str]] = {}
        for query in queries:
            page.goto(_FB_SEARCH_URL.format(query=quote(query)), wait_until="domcontentloaded")
            page.wait_for_timeout(_PAGE_SETTLE_MS + 1500)
            if self._login_wall(page, "facebook"):
                return None
            links = page.evaluate(
                "() => Array.from(document.querySelectorAll('div[role=\"main\"] a[href]'))"
                ".map(a => [a.href, (a.innerText || '').trim()])"
            )
            for href, title in links:
                lead = contacts.facebook_lead(href)
                if lead is None or lead.external_id in candidates or not title:
                    continue
                if name_matches(org.names, lead.external_id, title):
                    candidates[lead.external_id] = (lead, title)
            _pause()
            if candidates:
                break
        best = None
        for lead, title in list(candidates.values())[:_MAX_CANDIDATES]:
            page.goto(lead.group_url, wait_until="domcontentloaded")
            page.wait_for_timeout(_PAGE_SETTLE_MS + 1500)
            if self._login_wall(page, "facebook"):
                return None
            text = self._text(page, 'div[role="main"]') or ""
            result = match(org, lead.external_id, title, text)
            logger.info("social lookup: facebook %s → %s", lead.external_id, result.reasons or "нет совпадений")
            if result.ok and (best is None or result.score > best[1].score):
                best = (lead, result)
            _pause()
        return best

    # ---------- общее ----------
    def _page(self, channel: str, home_url: str | None) -> Page | None:
        page = self._pages.get(channel)
        if page is not None:
            return page
        kwargs = {"storage_state": self._sessions[channel], "user_agent": _USER_AGENT, "locale": "en-US"}
        if channel == "facebook" and settings.FB_PROXY:
            kwargs["proxy"] = _proxy(settings.FB_PROXY)
        context = self._browser.new_context(**kwargs)
        page = context.new_page()
        if home_url:
            page.goto(home_url, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            if self._login_wall(page, channel):
                context.close()
                return None
        self._pages[channel] = page
        return page

    def _login_wall(self, page: Page, channel: str) -> bool:
        text = (self._text(page, "body") or "").lower()[:3000]
        if "/accounts/login" in page.url or "/login" in page.url or any(m in text for m in _LOGIN_MARKERS):
            self._disable(channel, "session expired (login page shown)")
            return True
        return False

    def _disable(self, channel: str, reason: str) -> None:
        logger.warning("social lookup: %s disabled for this run — %s", channel, reason)
        self._disabled.add(channel)

    @staticmethod
    def _text(page: Page, selector: str) -> str | None:
        try:
            el = page.query_selector(selector)
            return el.inner_text() if el else None
        except Exception:  # noqa: BLE001 - страница посреди навигации
            return None


def _pause() -> None:
    time.sleep(random.uniform(settings.SOCIAL_LOOKUP_MIN_DELAY_SEC, settings.SOCIAL_LOOKUP_MAX_DELAY_SEC))


def _proxy(raw: str) -> dict:
    from urllib.parse import unquote, urlsplit

    if "://" not in raw:
        raw = f"http://{raw}"
    parts = urlsplit(raw)
    proxy = {"server": f"{parts.scheme}://{parts.hostname}:{parts.port}"}
    if parts.username:
        proxy["username"] = unquote(parts.username)
    if parts.password:
        proxy["password"] = unquote(parts.password)
    return proxy
