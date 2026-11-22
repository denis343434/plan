import logging
import random
import re
import time
from typing import Callable
from urllib.parse import quote

from playwright._impl._errors import TargetClosedError
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from app.adapters.base import (
    AntiBotChallengeError,
    BrowserCrashedError,
    CaptchaDetectedError,
    ParseFilters,
    RawLead,
)
from app.config import settings

logger = logging.getLogger(__name__)

# Первая попытка была сканировать весь видимый текст страницы группы регексом на домен —
# отбросили: VK показывает в сайдбаре ротирующуюся рекламу (случайные реальные домены типа
# "cian.ru", "school.ru"), это давало сплошные false positive почти на каждой группе.
# Вместо этого используем структурированное поле VK "официальный сайт сообщества"
# (data-testid="group-info-site", проверено вживую) — если оно заполнено, у группы есть
# сайт; regex здесь — валидация, что там реально доменоподобная строка, а не мусор/пусто.
_SITE_FIELD_SELECTOR = '[data-testid="group-info-site"]'
_SITE_DOMAIN_PATTERN = re.compile(r"[a-zа-яё0-9][a-zа-яё0-9-]*\.[a-zа-яё]{2,}", re.IGNORECASE)

# Проверено вживую 2026-08-20: старые "/search/groups?c[q]=" и SearchGroupsList__* — рудимент
# дореформенного VK, современный путь — /search/communities?q=.
#
# НО проверено вживую 2026-08-21 (парсинг кампании "Спортзал" молча вернул 0 групп): ПРЯМОЙ
# заход (page.goto) на "https://vk.com/search/communities?q=..." (и то же самое на vk.ru)
# сервер VK редиректит на битую legacy-страницу "/main.php?subdir=search&subsubdir=communities"
# с title="Ошибка" и пустым телом — не таймаут, не капча, просто пустая страница, отсюда
# "0 карточек" без единой ошибки в логах. Работает ТОЛЬКО переход изнутри уже загруженного
# SPA: набрать запрос в строке поиска шапки (как живой пользователь, см. _open_communities_search)
# и дойти до полного списка через блок "Сообщества" на странице общей выдачи → "Показать все".
# Только после такого перехода страница реально показывает карточки — VKUI RichCell с
# data-testid="group_item_desktop_list".
_FEED_URL = "https://vk.ru/feed"
# VK (редизайн, ноябрь 2026) убрал data-testid из всей вёрстки: на странице ленты/группы их
# остаётся 0-3 служебных. Строка поиска теперь обычный input[type="search"] в шапке (#ts_wrap),
# поэтому сначала ищем его, а старый data-testid оставлен вторым на случай отката/АБ.
_SEARCH_INPUT_SELECTOR = '#ts_wrap input[type="search"], [data-testid="search_top_input"]'
_RESULT_HEADER_SELECTOR = '[data-testid="header"]'
# Проверено вживую 2026-08-21: сразу после Enter в DOM ненадолго появляется лёгкая подсказка-
# автокомплит ("Недавние", "Люди", "Сообщества" — без кнопки "Показать все", без реальных
# карточек), и у неё ТОЖЕ есть заголовок с текстом "Сообщества" — ждать просто "заголовок с
# 'Сообщества'" ловило именно эту подсказку вместо настоящих результатов (0 карточек вместо
# 20+). Настоящая страница результатов приходит позже (~8-20с) и помечена заголовком
# "Результаты поиска" — ждать нужно именно его, только потом искать секцию "Сообщества".
_SEARCH_RESULTS_LABEL = "Результаты поиска"
_COMMUNITIES_SECTION_LABEL = "Сообщества"
# "Показать все" в секции "Сообщества" — ссылка на полный список сообществ. VK держит её href
# стабильным (/search/communities?q=...), в отличие от исчезнувших data-testid и вёрстки.
_SHOW_ALL_SELECTOR = 'a[href*="/search/communities"]'
_GROUP_CARD_SELECTOR = '[data-testid="group_item_desktop_list"]'
# Выдача VK подгружает карточки бесконечным скроллом порциями (~20 за раз) — без явного
# докручивания в DOM всегда только первая порция, независимо от filters.max_groups.
_SCROLL_WAIT_MS = 1500
_MAX_SCROLL_ATTEMPTS = 25
# Сколько подряд скроллов без роста DOM считать концом выдачи. Проверено вживую: изолированный
# вызов _scroll_until_enough_candidates обычно укладывается в один _SCROLL_WAIT_MS и честно
# доскролливает до max_groups, но в реальном цикле кампании (2026-08-21, max_groups=40)
# застревало на первой порции в 20 — VK иногда не успевает дорендерить следующую порцию за
# 1500мс, и это ошибочно принималось за "выдача кончилась". Требуем NO_GROWTH_STALLS_TO_STOP
# подряд пустых попыток (т.е. одну лишнюю паузу VK на "подумать"), прежде чем сдаться по-настоящему.
_NO_GROWTH_STALLS_TO_STOP = 2
# Проверено вживую 2026-10-11 (кампания "Ювелирный салон" — парсинг молча возвращал 0 групп):
# VK перешёл на хешированные CSS-классы ("meXqZJ vkitInternalRichAvatar ..."), прежние
# "a.vkuiAvatar__host" / '[class*="vkuiHeadline"]' из карточки пропали, и _parse_card отбрасывал
# КАЖДУЮ карточку. Классы больше не используем вовсе — только структуру: в карточке две ссылки на
# группу (аватар с aria-label = название, и текстовая ссылка-заголовок), обе с относительным href.
_GROUP_LINK_SELECTOR = 'a[href^="/"]'
_CAPTCHA_SELECTOR = "div.captcha, form[action*='captcha']"

# VK убрал data-testid, поэтому карточки выдачи/списка сообществ больше не находятся по
# [data-testid="group_item_desktop_list"]. Собираем ссылки структурно: ссылка на сообщество
# (/club123, /public123, /screen_name) в основном контенте (не в шапке и не в левом меню), у
# которой в пределах 8 предков есть «подписчик»/«Подписаться» — это карточка сообщества.
_COLLECT_CARDS_JS = """
() => {
  const out = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const href = (a.getAttribute('href') || '').split('?')[0];
    if (!/^\\/(club\\d+|public\\d+|event\\d+|[A-Za-z][A-Za-z0-9_.]{1,31})$/.test(href)) continue;
    if (a.closest('#layout_sidebar') || a.closest('#page_header')) continue;
    let node = a, isCard = false;
    for (let i = 0; i < 8 && node.parentElement; i++) {
      node = node.parentElement;
      if (/подписчик|Подписаться/i.test(node.textContent || '')) { isCard = true; break; }
    }
    if (!isCard) continue;
    out.push({href, title: (a.getAttribute('aria-label') || a.innerText || '').trim()});
  }
  return out;
}
"""

_HAS_GROUP_LINK_JS = """
() => Array.from(document.querySelectorAll('a[href]')).some((a) => {
  if (a.closest('#layout_sidebar') || a.closest('#page_header')) return false;
  const href = (a.getAttribute('href') || '').split('?')[0];
  return /^\\/(club\\d+|public\\d+|event\\d+|[A-Za-z][A-Za-z0-9_.]{1,31})$/.test(href);
})
"""

# Признак того, что страница группы отрисовала шапку (кнопка/счётчик подписчиков) — после этого
# можно читать блок контактов. Раньше ждали именно [data-testid="group-info-site"], которого
# больше нет; ждать "сайта" вообще нельзя (у большинства его нет).
_GROUP_HEADER_READY_JS = """
() => Array.from(document.querySelectorAll('*')).some(
  (e) => e.children.length === 0 && /^(Подписчики|Подписаться|Участники)$/.test((e.textContent || '').trim())
)
"""

# Сайт сообщества в новой вёрстке — ссылка-адрес в блоке контактов, у которой иконка глобуса
# (svg.vkuiIcon--globe_outline_20). Это самый надёжный признак: он не зависит от того, в какой
# колонке/на каком месте страницы VK нарисовал контакты (раньше проверка по координатам и длине
# контейнера пропускала часть групп с сайтом). Дополнительно смотрим описание сообщества — VK
# иногда показывает сайт только там. Мессенджеры/соцсети и короткие vk-ссылки сайтом не считаем.
_SITE_CHECK_JS = """
() => {
  const ignoreHost = /(^|\\.)(vk\\.(com|ru|me|link|cc)|userapi\\.com|vk\\.company|vk\\.wiki|t\\.me|telegram\\.me|wa\\.me|whatsapp\\.com|max\\.ru|ok\\.ru|instagram\\.com|facebook\\.com|youtube\\.com|youtu\\.be|dzen\\.ru|rutube\\.ru)$/i;
  const urlLike = /^(https?:\\/\\/)?[a-zа-яё0-9][a-zа-яё0-9.-]*\\.[a-zа-яё]{2,}(\\/.*)?$/i;
  // Известные TLD, включая punycode для кириллических доменов (.рф/.москва/.рус/.бел/...): VK
  // отдаёт такие адреса уже в punycode. Нужен только для описания — чтобы не спутать адрес
  // ("ул.Университетская", который VK тоже превращает в ссылку) с настоящим сайтом.
  const okTld = /^(xn--p1ai|xn--80adxhks|xn--p1acf|xn--90ais|ru|com|net|org|su|io|pro|shop|store|site|online|biz|info|me|club|tech|studio|digital|agency|center|group|company|design|art|space|xyz|top|moscow)$/i;
  const found = [];
  const push = (href, strict) => {
    let target = href || '';
    const m = target.match(/away\\.php\\?to=([^&]+)/);
    if (m) { try { target = decodeURIComponent(m[1]); } catch (e) {} }
    // Instagram в любом виде (instagram.com, www./l.instagram.com, instagr.am, ссылка вида
    // ".../instagram") — не собственный сайт сообщества: такую группу пишем как "без сайта".
    if (/instagram/i.test(target) || /instagr\\.am/i.test(target)) return;
    let host = '';
    try { host = new URL(target, 'https://vk.com').hostname; } catch (e) { host = ''; }
    if (!host || ignoreHost.test(host)) return;
    if (strict) {
      const tld = host.split('.').pop() || '';
      if (!okTld.test(tld)) return;
    }
    if (!found.includes(host)) found.push(host);
  };
  // 1) Строка "сайт" в контактах: ссылка с иконкой глобуса — так VK рисует именно сайт. Здесь
  //    доверяем самой иконке: домен может быть кириллическим (punycode) — это всё равно сайт.
  for (const a of document.querySelectorAll('a[href]')) {
    if (a.querySelector('svg[class*="globe"]')) push(a.getAttribute('href'), false);
  }
  // 2) Сайт, указанный в описании сообщества (VK показывает его там, если поле контактов пустое).
  for (const a of document.querySelectorAll('[elementtiming*="community_description"] a[href]')) {
    if (urlLike.test((a.innerText || '').trim())) push(a.getAttribute('href'), true);
  }
  return found;
}
"""

# Таймаут на признак "залогинены" короче, чем _SLOW_RENDER_TIMEOUT_MS в других местах файла —
# это ручная разовая проверка из панели (см. routers/accounts.py), а не фоновая задача, где
# лишние 15-20с ожидания незаметны. Если сессия жива, поисковая строка и так обычно
# отрисовывается за пару секунд; если сессии нет — VK либо сразу редиректит на экран выбора
# аккаунта, либо просто не рисует авторизованный хедер, ждать дольше нет смысла.
_SESSION_CHECK_TIMEOUT_MS = 15_000

# Экран VK "Выберите аккаунт для входа" / "Необходимо войти в аккаунт снова" — показывается
# вместо запрошенной страницы, когда сохранённый storage_state больше не действителен. Раньше
# явно не распознавался: код просто не находил ожидаемый элемент и после долгого таймаута шёл
# дальше по кандидатам, каждый раз впустую (см. запрос пользователя 2026-08-24 — то же самое
# уже чинили для messaging-service/app/adapters/vk.py, здесь дублируем по тому же принципу).
# НЕ проверено вживую под devtools — заведено по скриншоту, а не по разметке.
_LOGIN_REQUIRED_TEXT = "Выберите аккаунт для входа"

# Новая антибот-страница VK (challenge.html): "Проверяем, что вы не робот" + кнопка "Продолжить".
# Проходить её автоматически нельзя/бессмысленно — нужен видимый браузер и ручное действие.
_CHALLENGE_TEXT = "Проверяем, что вы не робот"

# Анонимный режим (use_account=False у кампании, см. app/tasks.py::run_parse_task) — SPA-поиск
# через шапку ленты (_open_communities_search выше) доступен только залогиненным: без cookies
# VK отдаёт на /feed публичную посадочную/логин-страницу, там нет ни ленты, ни строки поиска, и
# wait_for_selector на _SEARCH_INPUT_SELECTOR просто отвалится по таймауту. Единственный
# известный путь без аккаунта — классический URL поиска "c[section]=/c[q]=", годами открытый
# гостям (в отличие от современной SPA-выдачи, которую и обслуживает код выше).
#
# НЕ проверено вживую: ни этот URL, ни разметка результатов на нём не сверялись через devtools
# на реальном запросе. Вместо конкретных CSS-классов (которые почти наверняка со временем
# менялись и здесь были бы чистой угадайкой) используем структурный признак — ссылка вида
# "/короткое_имя" или "/club12345"/"/public12345", как у любого паблика VK — тот же приём, что
# уже применялся в этом файле для поля "сайт" (см. _SITE_FIELD_SELECTOR), только на разметку
# результатов легаси-поиска. При первом реальном прогоне анонимного режима свериться через
# VK_HEADLESS=false и обновить, если верстка окажется другой.
_ANON_SEARCH_URL_TEMPLATE = "https://vk.com/search?c%5Bsection%5D=communities&c%5Bq%5D={query}"
_ANON_RESULT_LINK_PATTERN = re.compile(r"^/(club\d+|public\d+|event\d+|[a-zA-Z][a-zA-Z0-9_.]{1,31})$")
# Технические внутренние ссылки VK, которые попадаются в шапке/подвале даже анонимному гостю и
# формально проходят под _ANON_RESULT_LINK_PATTERN — исключаем по имени сегмента, а не гадаем
# точный контейнер результатов, который на легаси-странице не проверен.
_ANON_IGNORED_PATH_SEGMENTS = {
    "search", "feed", "im", "settings", "id", "away", "help", "support", "privacy", "terms",
    "about", "apps", "video", "audio", "friends", "dev", "job", "logout", "login",
}


class SessionExpiredError(Exception):
    """VK показал экран повторного входа вместо запрошенной страницы — сохранённая сессия
    аккаунта протухла, дальнейший обход тем же браузером/аккаунтом бессмысленный."""


def _raise_if_login_required(page: Page) -> None:
    if page.locator(f"text={_LOGIN_REQUIRED_TEXT}").count() > 0:
        raise SessionExpiredError(
            "VK требует повторного входа — сохранённая сессия аккаунта протухла, "
            "нужно перелогиниться (кнопка «Войти в VK» в панели)"
        )


# Блокируем только то, что не влияет на DOM/селекторы, которые парсер реально читает (карточки
# поиска, поле сайта группы, строка поиска в шапке) — картинки/шрифты/видео и известные
# сторонние аналитику/трекеры. Специально НЕ трогаем сам JS/CSS VK — это SPA, без его скриптов
# ничего не отрисуется вообще, а не просто "будет некрасиво". См. parser-speedup-ideas.md,
# пункт 1 ("Резать вес страниц") — по этому же документу риск низкий (белый список типов
# ресурсов и доменов, которые режем, а не чёрный список того, что оставляем), эффект высокий.
_BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
_BLOCKED_URL_SUBSTRINGS = (
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "mc.yandex.ru",
    "top-fwz1.mail.ru",
    "top.mail.ru",
    "an.yandex.ru",
)


def _normalize_title(text: str) -> str:
    return " ".join(text.lower().replace("ё", "е").split())


def _is_excluded_title(title: str | None) -> bool:
    """True, если в названии группы есть стоп-фраза из VK_EXCLUDED_TITLE_PHRASES — такие группы
    (например, "Ищу модель" — объявления о поиске моделей, а не бизнес) не нужны как лиды."""
    if not title:
        return False
    normalized = _normalize_title(title)
    return any(
        phrase and phrase in normalized
        for phrase in (_normalize_title(p) for p in settings.VK_EXCLUDED_TITLE_PHRASES)
    )


def _is_driver_dead(exc: BaseException) -> bool:
    # Умер сам процесс драйвера Playwright (node.exe), а не браузер/вкладка: Playwright бросает
    # обычный Error "Connection closed while reading from the driver", НЕ TargetClosedError, и
    # раньше он ловился общим except в _has_external_site как сбой одного кандидата — цикл шёл
    # дальше, а следующий вызов в мёртвый драйвер навсегда вешал поток (задача висела в
    # running на 186/381, см. запрос пользователя 2026-10-23).
    return "Connection closed while reading from the driver" in str(exc)


def _is_browser_dead(exc: BaseException) -> bool:
    return isinstance(exc, TargetClosedError) or _is_driver_dead(exc)


def _block_heavy_resources(context) -> None:
    def handler(route) -> None:
        request = route.request
        if request.resource_type in _BLOCKED_RESOURCE_TYPES or any(
            s in request.url for s in _BLOCKED_URL_SUBSTRINGS
        ):
            route.abort()
            return
        route.continue_()

    context.route("**/*", handler)


def check_session(storage_state: dict | None) -> bool:
    """Реальная проверка через Playwright: жива ли сохранённая VK-сессия.

    Открывает vk.ru/feed с сохранённым storage_state и ждёт верхнюю строку поиска
    (_SEARCH_INPUT_SELECTOR — тот же признак "залогинен", что и в _open_communities_search) —
    она рисуется только в авторизованном интерфейсе VK, при протухшей сессии вместо этого
    показывается экран выбора аккаунта/повторного входа.
    """
    if not storage_state:
        return False
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=settings.VK_HEADLESS)
        context = browser.new_context(storage_state=storage_state)
        _block_heavy_resources(context)
        page = context.new_page()
        try:
            page.goto(_FEED_URL, wait_until="domcontentloaded")
            page.wait_for_selector(_SEARCH_INPUT_SELECTOR, timeout=_SESSION_CHECK_TIMEOUT_MS)
            return True
        except PlaywrightTimeoutError:
            return False
        finally:
            browser.close()


class VkParserAdapter:
    def __init__(self, storage_state: dict) -> None:
        self._storage_state = storage_state

    def search_communities(
        self,
        keyword: str,
        filters: ParseFilters,
        on_progress: Callable[[int, int, int], None] | None = None,
    ) -> list[RawLead]:
        leads: list[RawLead] = []
        # Не `with sync_playwright()`: его __exit__ (как и context/browser.close()) после смерти
        # драйвера может так и не вернуться — тогда собранные лиды не дошли бы до вызывающего
        # кода. При мёртвом драйвере закрытие просто пропускаем (закрывать уже нечего).
        pw = sync_playwright().start()
        driver_dead = False
        try:
            browser = pw.chromium.launch(headless=settings.VK_HEADLESS)
            context = browser.new_context(storage_state=self._storage_state or None)
            _block_heavy_resources(context)
            page = context.new_page()
            try:
                if self._storage_state:
                    self._open_communities_search(page, keyword, leads)

                    try:
                        page.wait_for_function(_HAS_GROUP_LINK_JS, timeout=15_000)
                    except PlaywrightTimeoutError:
                        pass  # ни одной группы по запросу — не ошибка, просто пустой результат

                    if filters.max_groups is not None:
                        self._scroll_until_enough_new_candidates(page, filters)

                    candidates = self._collect_candidates(page, filters)
                else:
                    candidates = self._search_communities_anonymous(page, keyword, filters, leads)

                # filters.has_site is True — явный запрос "только с сайтом", инвертируем;
                # False/None (дефолт) — обычное поведение парсера: сайт есть → лид не нужен
                # (весь смысл инструмента — искать сообщества БЕЗ своего сайта, см. architecture.md).
                want_site = filters.has_site is True
                total = len(candidates)
                if on_progress is not None:
                    on_progress(0, total, 0)
                for checked, lead in enumerate(candidates, start=1):
                    has_site = self._has_external_site(page, lead.group_url)
                    if has_site == want_site:
                        leads.append(lead)
                        self._sleep_random()
                    if on_progress is not None:
                        on_progress(checked, total, len(leads))
                    if filters.max_groups is not None and len(leads) >= filters.max_groups:
                        break  # набрали нужное количество — дальше кандидатов не проверяем
            except Exception as exc:
                if not _is_browser_dead(exc):
                    raise
                driver_dead = _is_driver_dead(exc)
                raise BrowserCrashedError(f"browser crashed: {exc}", partial_leads=leads) from exc
            finally:
                if not driver_dead:
                    try:
                        context.close()
                        browser.close()
                    except Exception:
                        # Браузер уже закрыт (TargetClosedError выше) — не затираем исходную ошибку.
                        logger.warning("failed to close VK browser", exc_info=True)
        finally:
            if not driver_dead:
                pw.stop()
        return leads

    def _open_communities_search(self, page: Page, keyword: str, collected_so_far: list[RawLead]) -> None:
        """Доводит page до выдачи сообществ по keyword — см. комментарий у _FEED_URL про то,
        почему нельзя просто page.goto() на URL поиска напрямую.

        Если по итогу карточек сообществ нет вовсе (ни одной в выдаче, либо секция
        "Сообщества" отсутствует, либо результатов настолько мало, что VK не показал кнопку
        "Показать все") — просто возвращаемся, вызывающий код (wait_for_selector на
        _GROUP_CARD_SELECTOR) корректно обработает и пустую, и уже готовую выдачу.
        """
        # wait_until="load" (дефолт) у VK почти всегда упирается в таймаут: страница — тяжёлый
        # SPA с фоновыми вебсокетами/long-polling (онлайн-статус, аналитика), из-за которых
        # событие "load" может не наступить вовсе, хотя сам контент уже отрисован.
        page.goto(_FEED_URL, wait_until="domcontentloaded")
        self._pass_challenge_if_visible(page)  # видимый браузер: ждём ручное «Продолжить»
        self._raise_if_captcha(page, collected_so_far)  # антибот-страница видна по URL сразу
        try:
            # Проверено вживую 2026-08-21: реальный контент VK в этом окружении иногда
            # прогружается только через ~18-20с после domcontentloaded (страница до этого
            # висит в скелетон-заглушке) — короткий таймаут тут стабильно ловил "не нашли".
            search_input = page.wait_for_selector(_SEARCH_INPUT_SELECTOR, timeout=30_000)
        except PlaywrightTimeoutError:
            # Проверяем ПОСЛЕ таймаута, а не сразу после goto — сразу после goto экран
            # повторного входа (если он есть) ещё мог не отрисоваться (та же задержка VK,
            # что и в комментарии выше), instant-проверка тогда всегда мимо (см. запрос
            # пользователя 2026-08-24 — первая версия этой проверки ничего не находила).
            _raise_if_login_required(page)
            self._raise_if_captcha(page, collected_so_far)  # антибот-страница тоже даёт таймаут
            raise RuntimeError("VK не догрузился — строка поиска в шапке не появилась за 30с")
        self._raise_if_captcha(page, collected_so_far)

        search_input.click()
        search_input.type(keyword, delay=random.uniform(30, 80))
        self._sleep_random()
        page.keyboard.press("Enter")

        # Ждём именно "Результаты поиска" (см. комментарий у _SEARCH_RESULTS_LABEL) — маркер
        # настоящей страницы результатов, а не мимолётной подсказки-автокомплита. Раньше это
        # был поиск заголовка по [data-testid="header"], теперь testid нет — берём текст.
        if not self._wait_for_text(page, _SEARCH_RESULTS_LABEL, timeout=20_000):
            return  # настоящая страница результатов не подгрузилась за 20с — 0 карточек
        self._raise_if_captcha(page, collected_so_far)

        # Секция "Сообщества" может дорисовываться уже ПОСЛЕ "Результаты поиска". Кнопка
        # "Показать все" ведёт на полный список сообществ (/search/communities?q=...) — ждать
        # заголовок секции отдельно больше не нужно, достаточно самой ссылки.
        show_all = page.query_selector(_SHOW_ALL_SELECTOR)
        if show_all is None:
            return  # все найденные сообщества уже показаны на странице результатов
        show_all.click()
        page.wait_for_timeout(2000)  # переход внутри SPA, не полная навигация — короткая пауза

    def _search_communities_anonymous(
        self, page: Page, keyword: str, filters: ParseFilters, collected_so_far: list[RawLead]
    ) -> list[RawLead]:
        """Анонимный аналог _open_communities_search (см. комментарий у _ANON_SEARCH_URL_TEMPLATE
        про то, почему это отдельный путь, а не переиспользование SPA-поиска). Докрутка выдачи
        здесь грубее, чем в авторизованной ветке — легаси-страница без проверенной вживую
        разметки, поэтому max_groups для анонимного режима стоит считать более приблизительным."""
        url = _ANON_SEARCH_URL_TEMPLATE.format(query=quote(keyword))
        try:
            page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:
            if _is_browser_dead(exc):
                raise  # браузер мёртв целиком — см. тот же приём и комментарий у _has_external_site
            logger.warning("anonymous VK search failed to load %s", url, exc_info=True)
            return []
        page.wait_for_timeout(2000)  # легаси-страница без SPA-роутинга — простая пауза на рендер
        self._raise_if_captcha(page, collected_so_far)

        if filters.max_groups is not None:
            self._scroll_anonymous_until_enough_new(page, filters)

        seen_ids: set[str] = set()
        candidates: list[RawLead] = []
        for link in page.query_selector_all("a[href]"):
            lead = self._parse_anon_link(link)
            if lead is None or lead.external_id in seen_ids:
                continue
            seen_ids.add(lead.external_id)
            if lead.external_id in filters.known_external_ids:
                continue
            candidates.append(lead)
        return candidates

    def _scroll_anonymous_until_enough_new(self, page: Page, filters: ParseFilters) -> None:
        # Тот же приём, что и _scroll_until_enough_new_candidates, но по общему count() ссылок
        # страницы вместо карточек SPA — легаси-страница может не иметь бесконечного скролла
        # вовсе (пагинация вместо докрутки), тогда цикл просто быстро остановится по stalls.
        def count_new() -> int:
            seen: set[str] = set()
            total = 0
            for link in page.query_selector_all("a[href]"):
                lead = self._parse_anon_link(link)
                if lead is None or lead.external_id in seen:
                    continue
                seen.add(lead.external_id)
                if lead.external_id not in filters.known_external_ids:
                    total += 1
            return total

        max_groups = filters.max_groups
        stalls = 0
        total_loaded = len(page.query_selector_all("a[href]"))
        for _ in range(_MAX_SCROLL_ATTEMPTS):
            if max_groups is not None and count_new() >= max_groups:
                return
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(_SCROLL_WAIT_MS)
            current_total = len(page.query_selector_all("a[href]"))
            if current_total == total_loaded:
                stalls += 1
                if stalls >= _NO_GROWTH_STALLS_TO_STOP:
                    return
                continue
            stalls = 0
            total_loaded = current_total

    def _parse_anon_link(self, link) -> RawLead | None:
        href = link.get_attribute("href") or ""
        path = href.split("?", 1)[0]
        for prefix in ("https://vk.com", "https://vk.ru", "http://vk.com", "http://vk.ru"):
            if path.startswith(prefix):
                path = path[len(prefix):]
                break
        if not _ANON_RESULT_LINK_PATTERN.match(path):
            return None
        external_id = path.lstrip("/")
        if external_id.split(".", 1)[0] in _ANON_IGNORED_PATH_SEGMENTS:
            return None
        title = (link.inner_text() or "").strip() or None
        if _is_excluded_title(title):
            return None
        return RawLead(external_id=external_id, group_url=f"https://vk.com/{external_id}", title=title)

    def _wait_for_text(self, page: Page, text: str, timeout: int) -> bool:
        """True, если за timeout мс на странице появился элемент с таким текстом. False — не
        дождались (не гадаем, что это значит — решает вызывающий код). Замена прежнему поиску
        по [data-testid="header"], который VK убрал из вёрстки."""
        try:
            page.wait_for_selector(f"text={text}", timeout=timeout)
            return True
        except PlaywrightTimeoutError:
            return False

    def _collect_candidates(self, page: Page, filters: ParseFilters, skip_known: bool = True) -> list[RawLead]:
        """Ссылки на сообщества из текущей выдачи (карточки ищем структурно, без data-testid —
        см. _COLLECT_CARDS_JS). Дубли (аватар и заголовок одной карточки) схлопываются по
        external_id, первая непустая подпись становится названием."""
        seen: set[str] = set()
        result: list[RawLead] = []
        for item in page.evaluate(_COLLECT_CARDS_JS) or []:
            href = (item.get("href") or "").split("?", 1)[0]
            external_id = href.lstrip("/")
            if not external_id or external_id in seen:
                continue
            seen.add(external_id)
            if skip_known and external_id in filters.known_external_ids:
                continue
            title = (item.get("title") or "").strip() or None
            if _is_excluded_title(title):
                continue
            result.append(RawLead(external_id=external_id, group_url=f"https://vk.com/{external_id}", title=title))
        return result

    def _scroll_until_enough_new_candidates(self, page: Page, filters: ParseFilters) -> None:
        # Хотим минимум max_groups карточек в DOM, которых ещё нет в known_external_ids —
        # раньше здесь считались ВСЕ карточки без разбора, и при повторном запуске с той же
        # выдачей VK (тот же keyword почти всегда возвращает один и тот же порядок сообществ)
        # докрутка останавливалась на первой же порции, целиком состоящей из уже известных
        # групп, и парсер каждый раз "находил" одно и то же вместо того, чтобы уйти дальше.
        def count_new() -> int:
            return len(self._collect_candidates(page, filters))

        def count_all() -> int:
            return len(self._collect_candidates(page, filters, skip_known=False))

        max_groups = filters.max_groups
        new_count = count_new()
        total_loaded = count_all()
        stalls = 0
        for _ in range(_MAX_SCROLL_ATTEMPTS):
            if max_groups is not None and new_count >= max_groups:
                return
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(_SCROLL_WAIT_MS)
            current_total = count_all()
            if current_total == total_loaded:
                stalls += 1
                if stalls >= _NO_GROWTH_STALLS_TO_STOP:
                    return  # несколько попыток подряд без роста — выдача действительно кончилась
                continue
            stalls = 0
            total_loaded = current_total
            new_count = count_new()

    def _has_external_site(self, page: Page, group_url: str) -> bool | None:
        # Лишний реальный переход на страницу каждого кандидата — дороже по времени и риску
        # антибана, чем просто разбор карточек поиска, но иначе сайт не проверить: в самой
        # выдаче поиска домен нигде не показывается.
        #
        # goto вынесен в отдельный try с одним ретраем и своим except — раньше его сбой (таймаут
        # ИЛИ сетевая ошибка/закрытие страницы) попадал в ту же ветку, что и "поле сайта не
        # появилось на загруженной странице", и группа с реальным сайтом при плохом интернете
        # молча помечалась как "сайта нет" (см. запрос пользователя). Возвращаем None ("не
        # удалось проверить"), а не False — вызывающий код в search_communities и так пропускает
        # кандидата при None (has_site == want_site never true для None), группа просто не попадёт
        # в этот прогон и будет заново кандидатом в следующем (не в known_external_ids).
        for attempt in range(2):
            try:
                page.goto(group_url, wait_until="domcontentloaded")
                break
            except Exception as exc:
                if not _is_browser_dead(exc):
                    if attempt == 0:
                        time.sleep(1.0)
                        continue
                    logger.warning("failed to load group page %s, skipping (unknown site status)", group_url, exc_info=True)
                    return None
                # Браузер/страница закрылись целиком (краш вкладки и т.п.), а не сбой конкретного
                # goto — ретраить и продолжать перебор кандидатов бессмысленно, КАЖДЫЙ следующий
                # goto в этом же прогоне упадёт той же ошибкой. Раньше это тихо ловилось общим
                # except ниже и цикл в search_communities долбил закрытый браузер по кандидату в
                # секунду, пока не выбирал весь список — на кампании в сотни групп так уходило
                # больше POLL_TIMEOUT_SEC оркестратора (см. запрос пользователя 2026-09-03, лог
                # 338 подряд TargetClosedError, "фаза: таймаут · ошибка: parser: running").
                # Пробрасываем наверх, чтобы весь search_communities прервался сразу. То же для
                # смерти самого драйвера (см. _is_driver_dead).
                raise

        self._pass_challenge_if_visible(page)
        try:
            page.wait_for_function(_GROUP_HEADER_READY_JS, timeout=settings.VK_SITE_CHECK_TIMEOUT_MS * 2)
        except PlaywrightTimeoutError:
            # Проверяем ПОСЛЕ таймаута, а не сразу после goto — см. тот же приём и комментарий
            # в _open_communities_search. Если это он — SessionExpiredError пробрасывается
            # наверх отсюда же (не ловится ниже: раз уж КАЖДЫЙ оставшийся кандидат в этом же
            # цикле получил бы ту же мёртвую сессию, см. запрос пользователя 2026-08-24).
            _raise_if_login_required(page)
            # Не дождались шапки — НЕ считаем это "сайта нет" (раньше так и было, из-за чего
            # группа с сайтом на медленной странице попадала в "без сайта"). None = не смогли
            # проверить, кандидат просто уйдёт на следующий прогон.
            logger.info("group header did not render for %s, skipping (unknown site status)", group_url)
            return None
        except Exception as exc:
            if _is_browser_dead(exc):
                raise  # см. комментарий у goto выше — браузер мёртв целиком, не сбой этого кандидата
            logger.warning("failed to check site presence for %s, skipping (unknown site status)", group_url, exc_info=True)
            return None
        try:
            return bool(page.evaluate(_SITE_CHECK_JS))
        except Exception as exc:
            if _is_browser_dead(exc):
                raise
            logger.warning("failed to read site block for %s, skipping (unknown site status)", group_url, exc_info=True)
            return None

    def _parse_card(self, card, filters: ParseFilters) -> RawLead | None:
        link = card.query_selector(_GROUP_LINK_SELECTOR)
        if link is None:
            return None
        href = link.get_attribute("href") or ""
        # href вида "/flowerssv.moscow?search_track_code=..." — отбрасываем query-строку,
        # иначе она попадёт в external_id и сломает дедупликацию по (platform, external_id).
        external_id = href.split("?", 1)[0].lstrip("/")
        if not external_id:
            return None

        # Название: aria-label аватара, иначе текст первой непустой ссылки карточки (заголовок).
        title = (link.get_attribute("aria-label") or "").strip() or None
        if title is None:
            for title_link in card.query_selector_all(_GROUP_LINK_SELECTOR):
                if text := (title_link.inner_text() or "").strip():
                    title = text
                    break
        if _is_excluded_title(title):
            return None

        return RawLead(
            external_id=external_id,
            group_url=f"https://vk.com/{external_id}",
            title=title,
        )

    def _is_challenge(self, page: Page) -> bool:
        url = page.url
        challenge_url = isinstance(url, str) and "challenge.html" in url
        try:
            challenge_locator_hit = page.locator(f"text={_CHALLENGE_TEXT}").count() > 0
        except TypeError:  # MagicMock в юнит-тестах вместо реального счётчика
            challenge_locator_hit = False
        return challenge_url or challenge_locator_hit

    def _pass_challenge_if_visible(self, page: Page, timeout_ms: int = 180_000) -> None:
        """В видимом браузере (VK_HEADLESS=false) даём человеку нажать «Продолжить» на
        антибот-странице VK и продолжаем прогон, вместо того чтобы сразу падать ошибкой.
        В headless нажать некому — там проверка сразу уходит наверх как AntiBotChallengeError."""
        if settings.VK_HEADLESS or not self._is_challenge(page):
            return
        logger.warning(
            "VK anti-bot challenge shown — ждём до %dс, пока вы нажмёте «Продолжить» в окне браузера",
            timeout_ms // 1000,
        )
        try:
            page.wait_for_function(
                "() => !location.pathname.includes('challenge')"
                " && !document.body.innerText.includes('Проверяем, что вы не робот')",
                timeout=timeout_ms,
            )
        except PlaywrightTimeoutError:
            pass  # не дождались — _raise_if_captcha ниже даст понятную ошибку

    def _raise_if_captcha(self, page: Page, collected_so_far: list[RawLead]) -> None:
        # Отдельно ловим новую антибот-страницу VK (challenge.html/"Проверяем, что вы не робот"):
        # в ней нет ни div.captcha, ни form[action*=captcha], поэтому раньше она выглядела как
        # "VK не догрузился за 30с" и уводила в поиск несуществующих проблем с селекторами.
        if self._is_challenge(page):
            raise AntiBotChallengeError(
                "VK показывает антибот-проверку «Проверяем, что вы не робот» — "
                "включите видимый браузер (VK_HEADLESS=false) и нажмите «Продолжить» вручную",
                collected_so_far,
            )
        if page.query_selector(_CAPTCHA_SELECTOR) is not None:
            raise CaptchaDetectedError("VK captcha/anti-bot check detected", collected_so_far)

    def _sleep_random(self) -> None:
        time.sleep(random.uniform(settings.MIN_DELAY_SEC, settings.MAX_DELAY_SEC))
