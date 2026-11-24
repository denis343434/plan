"""Вкладки «Яндекс карты» и «Google карты»: поиск организаций по городу и виду деятельности + рассылка
по тому каналу, который указан в карточке организации (Яндекс — VK / Telegram / WhatsApp,
Google — Facebook / Instagram / Telegram, с поиском заведения в соцсетях; см. MapSource).

Отдельный модуль, а не ещё пара сотен строк в app.py. Сам ничего не импортирует из app.py
(тот запускается как __main__ — повторный импорт выполнил бы его второй раз): всё нужное
(run_bg, flash_status, кэш аккаунтов, api_request и адреса сервисов) приходит через App.

Как это устроено на бэкенде:
  - кампания с platform="yandex_maps" (или "google_maps"), keyword = вид деятельности, parse_params =
    {"city", "country", "channels", "has_site", "whatsapp_by_phone"} (Data Service, миграция 0008;
    country — только у Яндекс Карт);
  - parser-service открывает Яндекс/Google Карты, обходит карточки и пишет по одному лиду на
    организацию с платформой канала (vk/tg/whatsapp/facebook) — первый по приоритету, что есть в карточке;
  - messaging-service рассылает как обычно, беря аккаунт по платформе лида.
"""

import itertools
import re
import time
import tkinter as tk
import webbrowser
from dataclasses import dataclass
from tkinter import messagebox, ttk
from urllib.parse import quote

# admin_contact лида карт заканчивается ссылкой на карточку: "… · Я.Карты: https://…"
# (у Google — "G.Карты: …"). Отдельного поля с URL в Data Service нет, поэтому достаём его
# отсюда — для клика по адресу этого достаточно.
_MAP_URL_RE = re.compile(r"(?:Я\.Карты|G\.Карты):\s*(https?://\S+)")
_NON_ADDRESS_PREFIXES = ("тел.", "сайт:", "я.карты:", "g.карты:")


def _contact_parts(contact: str | None) -> list[str]:
    return [part.strip() for part in (contact or "").split("·") if part.strip()]


def _map_url_from_contact(contact: str | None) -> str | None:
    match = _MAP_URL_RE.search(contact or "")
    return match.group(1) if match else None


def _address_from_contact(contact: str | None) -> str | None:
    for part in _contact_parts(contact):
        if not part.lower().startswith(_NON_ADDRESS_PREFIXES):
            return part
    return None


def _map_search_url(address: str | None, source: "MapSource") -> str | None:
    """Запасной вариант для старых лидов без ссылки на карточку — поиск карт по адресу."""
    if not address:
        return None
    if source.platform == "yandex_maps":
        return f"https://yandex.ru/maps/?text={quote(address)}"
    return f"https://www.google.com/maps/search/?api=1&query={quote(address)}"

@dataclass(frozen=True)
class MapSource:
    """Источник-карта: платформа кампании и её название в нужных падежах для подписей."""

    platform: str
    title: str  # «Яндекс карты» — вкладка и заголовки диалогов
    search_in: str  # «по Яндекс Картам»
    of: str  # «Яндекс Карт»
    on: str  # «на Яндекс Картах»
    channels: tuple[str, ...]  # каналы рассылки, которые предлагает вкладка
    # Каналы, в которые можно писать по телефону из карточки, если ссылки нет (галочки в форме).
    phone_channels: tuple[str, ...] = ()
    # Галочка «искать в соцсетях»: нет ссылки в карточке — ищем заведение в Instagram/Facebook по
    # названию со сверкой телефона/адреса и пишем в Telegram по номеру (parse_params.search_socials).
    social_search: bool = False
    # Выбор страны в форме (страна + город). У Яндекс Карт организации есть и за пределами
    # России (Беларусь, Казахстан, Турция, ОАЭ, Таиланд и т.д.), у Google — страна не нужна.
    country_search: bool = False


YANDEX = MapSource(
    "yandex_maps", "Яндекс карты", "по Яндекс Картам", "Яндекс Карт", "на Яндекс Картах",
    ("vk", "tg", "whatsapp"), phone_channels=("whatsapp",), country_search=True,
)
GOOGLE = MapSource(
    "google_maps", "Google карты", "по Google Картам", "Google Карт", "на Google Картах",
    ("facebook", "instagram", "tg"), social_search=True,
)

CHANNEL_LABELS = {"vk": "VK", "tg": "Telegram", "whatsapp": "WhatsApp", "facebook": "Facebook", "instagram": "Instagram"}

CITIES = [
    "Москва", "Санкт-Петербург", "Новосибирск", "Екатеринбург", "Казань", "Нижний Новгород",
    "Челябинск", "Красноярск", "Самара", "Уфа", "Ростов-на-Дону", "Омск", "Краснодар", "Воронеж",
    "Пермь", "Волгоград", "Саратов", "Тюмень", "Тольятти", "Ижевск", "Барнаул", "Ульяновск",
    "Иркутск", "Хабаровск", "Ярославль", "Владивосток", "Махачкала", "Томск", "Оренбург",
    "Кемерово", "Новокузнецк", "Рязань", "Набережные Челны", "Астрахань", "Пенза", "Киров",
    "Липецк", "Чебоксары", "Калининград", "Тула", "Курск", "Ставрополь", "Сочи", "Улан-Удэ",
    "Тверь", "Магнитогорск", "Брянск", "Иваново", "Белгород", "Сургут", "Владимир", "Архангельск",
    "Калуга", "Смоленск", "Мурманск", "Вологда", "Якутск", "Грозный", "Петрозаводск", "Кострома",
]

# Страны Яндекс Карт и крупные города каждой — только подсказки в выпадающих списках. Страну и
# город можно ввести вручную: у Яндекс Карт есть организации и в странах, которых нет в списке.
CITIES_BY_COUNTRY: dict[str, list[str]] = {
    "Россия": CITIES,
    "Беларусь": [
        "Минск", "Гомель", "Гродно", "Витебск", "Брест", "Могилёв", "Бобруйск", "Барановичи",
        "Пинск", "Орша",
    ],
    "Казахстан": [
        "Алматы", "Астана", "Шымкент", "Караганда", "Актобе", "Атырау", "Тараз", "Павлодар",
        "Усть-Каменогорск", "Семей", "Костанай", "Кызылорда", "Актау", "Уральск", "Петропавловск",
    ],
    "Узбекистан": [
        "Ташкент", "Самарканд", "Бухара", "Наманган", "Андижан", "Фергана", "Нукус", "Термез",
        "Карши", "Джизак",
    ],
    "Кыргызстан": ["Бишкек", "Ош", "Джалал-Абад", "Каракол", "Чолпон-Ата", "Нарын", "Талас"],
    "Таджикистан": ["Душанбе", "Ходжент", "Куляб", "Бохтар", "Истаравшан"],
    "Туркменистан": ["Ашхабад", "Туркменабад", "Дашогуз", "Мары"],
    "Азербайджан": ["Баку", "Гянджа", "Сумгаит", "Мингечевир", "Нахичевань", "Шеки"],
    "Армения": ["Ереван", "Гюмри", "Ванадзор", "Вагаршапат", "Дилижан", "Капан"],
    "Грузия": ["Тбилиси", "Батуми", "Кутаиси", "Рустави", "Гори", "Зугдиди", "Телави"],
    "Молдова": ["Кишинёв", "Бельцы", "Тирасполь", "Бендеры", "Комрат", "Кагул"],
    "Украина": [
        "Киев", "Харьков", "Одесса", "Днепр", "Львов", "Запорожье", "Винница", "Полтава",
        "Черновцы", "Ивано-Франковск",
    ],
    "Сербия": ["Белград", "Нови-Сад", "Ниш", "Крагуевац", "Суботица"],
    "Турция": [
        "Стамбул", "Анталья", "Анкара", "Измир", "Бодрум", "Аланья", "Кемер", "Мерсин", "Бурса",
    ],
    "ОАЭ": ["Дубай", "Абу-Даби", "Шарджа", "Аджман", "Рас-эль-Хайма", "Фуджейра"],
    "Кипр": ["Лимасол", "Никосия", "Пафос", "Ларнака", "Айя-Напа", "Протарас"],
    "Таиланд": ["Пхукет", "Бангкок", "Паттайя", "Чиангмай", "Самуи", "Хуахин"],
    "Египет": ["Хургада", "Шарм-эль-Шейх", "Каир", "Александрия", "Марса-Алам"],
    "Израиль": ["Тель-Авив", "Иерусалим", "Хайфа", "Нетания", "Эйлат", "Ашдод"],
    "Германия": ["Берлин", "Мюнхен", "Гамбург", "Франкфурт", "Кёльн", "Дюссельдорф", "Штутгарт"],
    "Франция": ["Париж", "Ницца", "Лион", "Марсель", "Канны"],
    "Испания": ["Барселона", "Мадрид", "Валенсия", "Аликанте", "Малага"],
    "Италия": ["Рим", "Милан", "Венеция", "Флоренция", "Неаполь"],
    "Великобритания": ["Лондон", "Манчестер", "Бирмингем", "Эдинбург"],
    "США": ["Нью-Йорк", "Лос-Анджелес", "Майами", "Чикаго", "Сан-Франциско", "Лас-Вегас"],
    "Хорватия": ["Загреб", "Сплит", "Риека", "Дубровник"],
    "Польша": ["Варшава", "Краков", "Вроцлав", "Гданьск", "Познань", "Лодзь"],
    "Чехия": ["Прага", "Брно", "Острава", "Пльзень", "Карловы Вары"],
    "Литва": ["Вильнюс", "Каунас", "Клайпеда", "Шяуляй", "Паланга"],
    "Латвия": ["Рига", "Даугавпилс", "Лиепая", "Юрмала", "Елгава"],
    "Эстония": ["Таллин", "Тарту", "Нарва", "Пярну"],
    "Финляндия": ["Хельсинки", "Тампере", "Турку", "Лаппеэнранта"],
    "Черногория": ["Будва", "Подгорица", "Бар", "Котор", "Херцег-Нови"],
    "Болгария": ["София", "Варна", "Бургас", "Пловдив"],
    "Греция": ["Афины", "Салоники", "Патры", "Ираклион"],
    "Монголия": ["Улан-Батор", "Эрдэнэт", "Дархан"],
    "Китай": ["Пекин", "Шанхай", "Гуанчжоу", "Шэньчжэнь", "Харбин"],
    "Южная Корея": ["Сеул", "Пусан", "Инчхон"],
    "Япония": ["Токио", "Осака", "Киото", "Нагоя", "Саппоро"],
    "Вьетнам": ["Ханой", "Хошимин", "Нячанг", "Дананг", "Фукуок"],
    "Индонезия": ["Денпасар", "Джакарта", "Сурабая", "Бандунг", "Джокьякарта"],
    "Малайзия": ["Куала-Лумпур", "Пенанг", "Джохор-Бару", "Кота-Кинабалу"],
    "Индия": ["Дели", "Мумбаи", "Гоа", "Бангалор"],
    "Саудовская Аравия": ["Эр-Рияд", "Джидда", "Мекка", "Медина", "Даммам"],
    "Катар": ["Доха", "Аль-Вакра", "Аль-Хор"],
    "Австралия": ["Сидней", "Мельбурн", "Брисбен"],
    "Марокко": ["Касабланка", "Марракеш", "Агадир"],
}

COUNTRIES = list(CITIES_BY_COUNTRY)
DEFAULT_COUNTRY = "Россия"
# Все города из подсказок — по ним понимаем, что заполненный город относится к прежней стране
# (и его надо сбросить), а не введён вручную (такой не трогаем).
KNOWN_CITIES = {city for cities in CITIES_BY_COUNTRY.values() for city in cities}

ACTIVITIES = [
    "кафе", "ресторан", "кофейня", "пекарня", "доставка еды", "салон красоты", "парикмахерская",
    "барбершоп", "маникюр", "косметология", "массаж", "фитнес-клуб", "йога", "танцевальная студия",
    "стоматология", "медицинский центр", "ветеринарная клиника", "зоомагазин", "автосервис",
    "шиномонтаж", "автомойка", "детейлинг", "строительная компания", "ремонт квартир",
    "мебель на заказ", "окна", "натяжные потолки", "клининг", "химчистка", "ремонт телефонов",
    "цветы", "магазин одежды", "ювелирный магазин", "фотостудия", "event-агентство",
    "детский центр", "языковая школа", "автошкола", "репетиторы", "юридические услуги",
    "бухгалтерские услуги", "агентство недвижимости", "гостиница", "хостел", "турагентство",
    "типография", "грузоперевозки", "эвакуатор", "кальянная", "баня", "сауна",
]

RU_LEAD_STATUS = {
    "new": "новый",
    "queued": "в очереди",
    "contacted": "написали",
    "replied": "ответили",
    "rejected": "отклонён",
}
RU_PHASE = {
    "idle": "ожидание",
    "parsing": "поиск на картах",
    "messaging": "рассылка",
    "done": "готово",
    "failed": "ошибка",
    "timeout": "таймаут",
}
RU_CAMPAIGN_STATUS = {"draft": "черновик", "running": "выполняется", "paused": "приостановлена", "completed": "завершена"}

# A/B — варианты первого сообщения (Messaging Service чередует их между лидами), F — повторное
# сообщение тем, кто не ответил; F никогда не уходит как первое (см. messaging-service/app/tasks.py).
TEMPLATE_VARIANTS = ["A", "B", "F"]
FOLLOWUP_VARIANT = "F"
DEFAULT_MESSAGE = "Здравствуйте, {{org_name}}!"

REFRESH_MS = 3000
# Аккаунт пригоден для рассылки, если у него есть сохранённая сессия, назначение
# messaging/both и он не забанен/не на паузе.
_USABLE_ACCOUNT_STATUSES = {"active", "locked"}


def _priority_orders(channels: list[str]) -> list[tuple[str, ...]]:
    return list(itertools.permutations(channels))


def _order_label(order: tuple[str, ...]) -> str:
    return " → ".join(CHANNEL_LABELS[c] for c in order)


def _place_label(city: str, country: str) -> str:
    """«Казань» для России (страна по умолчанию) и «Алматы, Казахстан» для остальных."""
    if country and country != DEFAULT_COUNTRY:
        return f"{city}, {country}" if city else country
    return city


class YandexMapsTab(ttk.Frame):
    def __init__(self, parent, app, api_request, urls: dict[str, str], source: MapSource = YANDEX) -> None:
        super().__init__(parent, padding=10)
        self.source = source
        self.app = app
        self.api = api_request
        self.data_url = urls["data"]
        self.orchestrator_url = urls["orchestrator"]
        self.messaging_url = urls["messaging"]

        self.selected_id: str | None = None
        self._campaigns: dict[str, dict] = {}
        self._send_campaign_id: str | None = None
        self._followup_campaign_id: str | None = None
        self._templates: dict[str, dict] = {}
        # (url, monotonic) последнего открытия карты: второй клик двойного клика по колонке
        # адреса не должен открывать вторую вкладку того же заведения.
        self._last_map_open: tuple[str | None, float] = (None, 0.0)
        # True, пока пользователь сам не трогал галочки каналов — до этого они следуют за
        # наличием аккаунтов ("смотря что есть"), потом — его выбор не перетираем.
        self._channels_auto = True
        # Страна, для которой сейчас показаны подсказки городов (см. _on_country_changed).
        self._city_country = DEFAULT_COUNTRY
        self.country: ttk.Combobox | None = None

        self.columnconfigure(0, weight=0)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)
        self._build_form().grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self._build_right().grid(row=0, column=1, sticky="nsew")

        self.after(500, self._tick)

    # ---------- layout ----------
    def _build_form(self) -> ttk.LabelFrame:
        panel = ttk.LabelFrame(self, text=f"Новый поиск {self.source.search_in}", padding=10)
        panel.columnconfigure(1, weight=1)
        row = itertools.count()

        if self.source.country_search:
            r = next(row)
            ttk.Label(panel, text="Страна:").grid(row=r, column=0, sticky="w", pady=3)
            self.country = ttk.Combobox(panel, values=COUNTRIES, width=28)
            self.country.set(DEFAULT_COUNTRY)
            self.country.grid(row=r, column=1, sticky="ew", pady=3)
            # Выбор из списка меняет подсказки городов; FocusOut — если страну ввели руками.
            self.country.bind("<<ComboboxSelected>>", self._on_country_changed)
            self.country.bind("<FocusOut>", self._on_country_changed)

        r = next(row)
        ttk.Label(panel, text="Город:").grid(row=r, column=0, sticky="w", pady=3)
        self.city = ttk.Combobox(panel, values=CITIES_BY_COUNTRY[DEFAULT_COUNTRY], width=28)
        self.city.grid(row=r, column=1, sticky="ew", pady=3)
        if self.source.country_search:
            ttk.Label(
                panel,
                text="Можно выбрать из списка или ввести вручную",
                foreground="#999", font=("Segoe UI", 8), wraplength=250,
            ).grid(row=next(row), column=1, sticky="w")

        r = next(row)
        ttk.Label(panel, text="Вид деятельности:").grid(row=r, column=0, sticky="w", pady=3)
        self.activity = ttk.Combobox(panel, values=ACTIVITIES, width=28)
        self.activity.grid(row=r, column=1, sticky="ew", pady=3)

        r = next(row)
        ttk.Label(panel, text="Найти организаций:").grid(row=r, column=0, sticky="w", pady=3)
        self.limit = ttk.Entry(panel, width=8)
        self.limit.insert(0, "30")
        self.limit.grid(row=r, column=1, sticky="w", pady=3)

        r = next(row)
        ttk.Label(panel, text="Название кампании:").grid(row=r, column=0, sticky="w", pady=3)
        self.name = ttk.Entry(panel)
        self.name.grid(row=r, column=1, sticky="ew", pady=3)
        ttk.Label(panel, text="пусто — «вид деятельности — город»", foreground="#999", font=("Segoe UI", 8)).grid(
            row=next(row), column=1, sticky="w"
        )

        r = next(row)
        ttk.Label(panel, text="КАНАЛЫ РАССЫЛКИ", font=("Segoe UI", 9, "bold"), foreground="#777").grid(
            row=r, column=0, columnspan=2, sticky="w", pady=(10, 2)
        )
        self.channel_vars: dict[str, tk.BooleanVar] = {}
        self.channel_account_labels: dict[str, ttk.Label] = {}
        for channel in self.source.channels:
            label = CHANNEL_LABELS[channel]
            r = next(row)
            var = tk.BooleanVar(value=True)
            self.channel_vars[channel] = var
            ttk.Checkbutton(panel, text=label, variable=var, command=self._on_channels_changed).grid(
                row=r, column=0, sticky="w"
            )
            acc_label = ttk.Label(panel, text="", foreground="#999")
            acc_label.grid(row=r, column=1, sticky="w")
            self.channel_account_labels[channel] = acc_label

        r = next(row)
        ttk.Label(panel, text="Приоритет:").grid(row=r, column=0, sticky="w", pady=3)
        self.priority = ttk.Combobox(panel, state="readonly", width=30)
        self.priority.grid(row=r, column=1, sticky="ew", pady=3)
        ttk.Label(
            panel,
            text="Каждой организации пишем один раз — в первый\nканал из списка, который указан в её карточке",
            foreground="#999", font=("Segoe UI", 8),
        ).grid(row=next(row), column=0, columnspan=2, sticky="w")

        r = next(row)
        self.only_without_site = tk.BooleanVar(value=True)
        ttk.Checkbutton(panel, text="Только организации без сайта", variable=self.only_without_site).grid(
            row=r, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )
        # По умолчанию включено: многие организации в Картах не указывают ссылку wa.me, только
        # мобильный номер — без этой галочки такие лиды молча пропускались. Номер всё равно
        # должен выглядеть мобильным (городские/стационарные отсекаются, см.
        # contacts.is_mobile_for_whatsapp) — при желании галочку можно снять.
        self.wa_by_phone = tk.BooleanVar(value=True)
        if "whatsapp" in self.source.phone_channels:
            ttk.Checkbutton(
                panel, text="WhatsApp по мобильному номеру, если нет ссылки", variable=self.wa_by_phone
            ).grid(row=next(row), column=0, columnspan=2, sticky="w")
        # У Google Карт ссылки на соцсети в карточках почти не бывает (проверено вживую
        # 2026-10-04: 0 из 119 ресторанов) — без поиска лидов не будет, поэтому включено сразу.
        self.search_socials = tk.BooleanVar(value=self.source.social_search)
        if self.source.social_search:
            ttk.Checkbutton(
                panel, text="Нет ссылки — искать в Facebook / Instagram по названию\n"
                "(со сверкой телефона/адреса), в Telegram — по номеру",
                variable=self.search_socials,
            ).grid(row=next(row), column=0, columnspan=2, sticky="w")

        r = next(row)
        ttk.Label(panel, text="ШАБЛОН СООБЩЕНИЯ", font=("Segoe UI", 9, "bold"), foreground="#777").grid(
            row=r, column=0, columnspan=2, sticky="w", pady=(10, 2)
        )
        r = next(row)
        variant_row = ttk.Frame(panel)
        variant_row.grid(row=r, column=0, columnspan=2, sticky="ew")
        ttk.Label(variant_row, text="Вариант:").pack(side="left")
        self.tpl_variant = ttk.Combobox(
            variant_row, values=TEMPLATE_VARIANTS, width=4, state="readonly"
        )
        self.tpl_variant.set("A")
        self.tpl_variant.pack(side="left", padx=(4, 0))
        self.tpl_variant.bind("<<ComboboxSelected>>", self._on_variant_selected)
        ttk.Label(
            variant_row, text="F — повторное сообщение", foreground="#999", font=("Segoe UI", 8)
        ).pack(side="left", padx=(8, 0))

        r = next(row)
        self.message = tk.Text(panel, height=5, width=40, wrap="word", font=("Segoe UI", 9))
        self.message.grid(row=r, column=0, columnspan=2, sticky="ew")
        self.message.insert("1.0", DEFAULT_MESSAGE)
        ttk.Label(
            panel, text="{{org_name}} — название организации", foreground="#999", font=("Segoe UI", 8)
        ).grid(row=next(row), column=0, columnspan=2, sticky="w")

        r = next(row)
        tpl_btns = ttk.Frame(panel)
        tpl_btns.grid(row=r, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Button(tpl_btns, text="Сохранить шаблон", command=self.on_save_template).pack(side="left")
        ttk.Button(tpl_btns, text="Удалить шаблон", style="Danger.TButton", command=self.on_delete_template).pack(
            side="left", padx=(6, 0)
        )

        r = next(row)
        btns = ttk.Frame(panel)
        btns.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Button(btns, text="Найти и разослать", style="Accent.TButton", command=lambda: self.on_create(True)).pack(
            side="left"
        )
        ttk.Button(btns, text="Только найти", command=lambda: self.on_create(False)).pack(side="left", padx=(6, 0))

        # Отдельной строкой, чтобы три кнопки не вылезали за узкую левую панель. Как кнопка
        # «Создать» в VK-вкладке: кампания появляется в списке черновиком, парсинг и рассылка
        # не стартуют — можно проверить настройки/шаблон и запустить позже.
        r = next(row)
        create_btn = ttk.Frame(panel)
        create_btn.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        ttk.Button(
            create_btn, text="Создать кампанию", command=lambda: self.on_create(False, start=False)
        ).pack(side="left")

        self._refresh_priority_choices()
        return panel

    def _build_right(self) -> ttk.Frame:
        frame = ttk.Frame(self)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(4, weight=1)

        camp_box = ttk.LabelFrame(frame, text=f"Кампании {self.source.of}", padding=8)
        camp_box.grid(row=0, column=0, sticky="ew")
        cols = ("name", "city", "activity", "channels", "status")
        self.camp_tree = ttk.Treeview(camp_box, columns=cols, show="headings", height=6, selectmode="browse")
        city_title = "Город / страна" if self.source.country_search else "Город"
        for col, title, width in (
            ("name", "Название", 200),
            ("city", city_title, 140),
            ("activity", "Вид деятельности", 140),
            ("channels", "Каналы", 170),
            ("status", "Статус", 100),
        ):
            self.camp_tree.heading(col, text=title)
            self.camp_tree.column(col, width=width, anchor="w")
        self.camp_tree.pack(fill="x")
        self.camp_tree.bind("<<TreeviewSelect>>", self._on_select)

        actions = ttk.Frame(camp_box)
        actions.pack(fill="x", pady=(6, 0))
        # Три режима запуска ВЫБРАННОЙ кампании (создать её — кнопкой «Создать кампанию» слева):
        # только парсинг, только рассылка по уже найденным, либо оба этапа подряд.
        ttk.Button(actions, text="Запустить только парсер", command=self.on_restart).pack(side="left")
        ttk.Button(actions, text="Запустить только рассылку", command=self.on_send).pack(side="left", padx=(6, 0))
        ttk.Button(
            actions, text="Запустить всё вместе", style="Success.TButton", command=self.on_start_all
        ).pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="Стоп", style="Danger.TButton", command=self.on_stop_send).pack(
            side="left", padx=(6, 0)
        )

        extra_actions = ttk.Frame(camp_box)
        extra_actions.pack(fill="x", pady=(4, 0))
        ttk.Button(extra_actions, text="Повторить с ошибками", command=lambda: self.on_send(retry_failed=True)).pack(
            side="left"
        )
        ttk.Button(extra_actions, text="Отправить повторные", command=self.on_send_followups).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(extra_actions, text="Обновить", command=self.on_refresh).pack(side="left", padx=(6, 0))
        ttk.Button(extra_actions, text="Удалить", style="Danger.TButton", command=self.on_delete).pack(
            side="left", padx=(6, 0)
        )

        self.status_label = ttk.Label(frame, text="Выберите кампанию или создайте новую слева", foreground="#555")
        self.status_label.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.stats_label = ttk.Label(frame, text="", font=("Segoe UI", 10))
        self.stats_label.grid(row=2, column=0, sticky="ew", pady=(2, 6))

        tpl_box = ttk.LabelFrame(
            frame, text="Шаблоны кампании (клик — загрузить в редактор слева)", padding=8
        )
        tpl_box.grid(row=3, column=0, sticky="ew", pady=(0, 6))
        self.tpl_tree = ttk.Treeview(
            tpl_box, columns=("variant", "body"), show="headings", height=3, selectmode="browse"
        )
        for col, title, width in (("variant", "Вариант", 70), ("body", "Текст", 650)):
            self.tpl_tree.heading(col, text=title)
            self.tpl_tree.column(col, width=width, anchor="w")
        self.tpl_tree.pack(fill="x")
        self.tpl_tree.bind("<<TreeviewSelect>>", self._on_template_row_selected)

        leads_box = ttk.LabelFrame(
            frame,
            text="Найденные организации (клик по адресу — карта, двойной клик — контакт)",
            padding=8,
        )
        leads_box.grid(row=4, column=0, sticky="nsew")
        cols = ("title", "channel", "url", "info", "status")
        self.leads_tree = ttk.Treeview(leads_box, columns=cols, show="headings")
        for col, title, width in (
            ("title", "Организация", 180),
            ("channel", "Канал", 75),
            ("url", "Контакт", 190),
            ("info", "Телефон / адрес (клик — карта)", 300),
            ("status", "Статус", 90),
        ):
            self.leads_tree.heading(col, text=title)
            self.leads_tree.column(col, width=width, anchor="w")
        scroll = ttk.Scrollbar(leads_box, orient="vertical", command=self.leads_tree.yview)
        self.leads_tree.configure(yscrollcommand=scroll.set)
        self.leads_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.leads_tree.bind("<Double-1>", self._on_lead_double_click)
        # Одиночный клик по колонке «Инфо / адрес» открывает организацию на карте, курсор над
        # ней — «рука». Двойной клик по остальным колонкам по-прежнему открывает чат.
        self.leads_tree.bind("<Button-1>", self._on_lead_click)
        self.leads_tree.bind("<Motion>", self._on_lead_motion)
        return frame

    # ---------- каналы / аккаунты ----------
    def _usable_accounts(self) -> dict[str, int]:
        counts = {c: 0 for c in self.source.channels}
        for a in self.app._accounts_cache.values():
            if (
                a.get("platform") in counts
                and a.get("purpose") in ("messaging", "both")
                and a.get("status") in _USABLE_ACCOUNT_STATUSES
                and a.get("has_session")
            ):
                counts[a["platform"]] += 1
        return counts

    def _refresh_accounts_hint(self) -> None:
        counts = self._usable_accounts()
        for channel, label in self.channel_account_labels.items():
            n = counts[channel]
            label.configure(
                text=f"аккаунтов: {n}" if n else "нет аккаунта со входом",
                foreground="#2e9e4f" if n else "#c0392b",
            )
        if self._channels_auto and self.app._accounts_cache:
            # "смотря что есть": по умолчанию включены только каналы, для которых есть чем писать.
            available = [c for c in self.source.channels if counts[c]]
            for channel, var in self.channel_vars.items():
                var.set(channel in available if available else True)
            self._refresh_priority_choices()

    def _on_channels_changed(self) -> None:
        self._channels_auto = False
        self._refresh_priority_choices()

    def _selected_channels(self) -> list[str]:
        return [c for c, var in self.channel_vars.items() if var.get()]

    def _refresh_priority_choices(self) -> None:
        channels = self._selected_channels()
        orders = _priority_orders(channels) if channels else []
        labels = [_order_label(o) for o in orders]
        current = self.priority.get()
        self.priority.configure(values=labels)
        self._priority_map = dict(zip(labels, orders))
        if current not in self._priority_map:
            self.priority.set(labels[0] if labels else "")

    # ---------- страна / город ----------
    def _on_country_changed(self, _event=None) -> None:
        country = self._country_value()
        if country == self._city_country:
            return
        self._city_country = country
        values = CITIES_BY_COUNTRY.get(country, [])
        self.city.configure(values=values)
        # Город из подсказок другой страны сбрасываем — иначе запрос уйдёт не туда. Введённый
        # вручную город (которого нет ни в одном списке) не трогаем: он может быть настоящим.
        city = self.city.get().strip()
        if city and city not in values and city in KNOWN_CITIES:
            self.city.set("")

    def _country_value(self) -> str:
        return self.country.get().strip() if self.country is not None else ""

    # ---------- создание / запуск ----------
    def on_create(self, with_send: bool, start: bool = True) -> None:
        city = self.city.get().strip()
        country = self._country_value()
        activity = self.activity.get().strip()
        if not city or not activity:
            messagebox.showwarning(self.source.title, "Укажите город и вид деятельности")
            return
        order = self._priority_map.get(self.priority.get())
        if not order:
            messagebox.showwarning(self.source.title, "Выберите хотя бы один канал рассылки")
            return
        limit = 0
        if start:
            # «Найти организаций» — это max_groups для запуска парсинга; при простом создании
            # кампании (без старта) значение не нужно и не мешает сохранить её заранее.
            try:
                limit = int(self.limit.get().strip() or 0)
            except ValueError:
                messagebox.showwarning(self.source.title, "«Найти организаций» должно быть числом")
                return
        body = self.message.get("1.0", "end").strip()
        variant = self.tpl_variant.get()
        if with_send and not body:
            messagebox.showwarning(self.source.title, "Введите текст сообщения")
            return
        if with_send and variant == FOLLOWUP_VARIANT:
            messagebox.showwarning(
                self.source.title,
                "Вариант F — это повторное сообщение. Для первой рассылки выберите A или B.",
            )
            return

        if with_send:
            counts = self._usable_accounts()
            missing = [CHANNEL_LABELS[c] for c in order if not counts[c]]
            if missing and not messagebox.askyesno(
                self.source.title,
                f"Нет аккаунта со входом для: {', '.join(missing)}.\n\n"
                "Организации с этим каналом будут найдены, но рассылка остановится на них "
                "(«нет свободного аккаунта»). Продолжить?",
            ):
                return

        parse_params = {
            "city": city,
            "channels": list(order),
            "has_site": False if self.only_without_site.get() else None,
            "whatsapp_by_phone": self.wa_by_phone.get(),
            "search_socials": self.search_socials.get(),
        }
        if self.source.country_search:
            # Пустая страна — старый режим «только город» (кампании, созданные до выбора страны).
            parse_params["country"] = country or None

        name = self.name.get().strip() or f"{activity} — {_place_label(city, country)}"
        payload = {
            "name": name,
            "platform": self.source.platform,
            "keyword": activity,
            "use_account": False,
            "parse_params": parse_params,
        }

        def create_and_start():
            campaign = self.api(self.orchestrator_url, "/campaigns", method="POST", body=payload)
            if not start:
                # «Создать кампанию» — только черновик: ни парсинг, ни шаблон не сохраняем.
                # Шаблон сохраняется отдельной кнопкой (или при запуске рассылки), иначе
                # последующий «Искать ещё» через Orchestrator сразу начал бы и рассылку.
                return campaign
            if with_send:
                self.api(
                    self.data_url, "/templates", method="POST",
                    body={"campaign_id": campaign["id"], "variant": variant, "body": body},
                )
            # Без шаблона оркестратор сам завершит кампанию после парсинга ("Только найти").
            path = f"/campaigns/{campaign['id']}/start"
            if limit > 0:
                path += f"?max_groups={limit}"
            self.api(self.orchestrator_url, path, method="POST")
            return campaign

        def done(campaign):
            if start:
                self.app.flash_status(f"Кампания «{campaign['name']}» запущена: ищу {self.source.on}…")
            else:
                self.app.flash_status(
                    f"Кампания «{campaign['name']}» создана — запустить можно кнопкой «Искать ещё»"
                )
            self.name.delete(0, "end")
            self.selected_id = campaign["id"]
            self.refresh_campaigns()

        def error(exc):
            messagebox.showerror(self.source.title, str(exc))

        self.app.run_bg(create_and_start, on_done=done, on_error=error)

    def _start_path(self, campaign_id: str, parse_only: bool) -> str:
        """URL запуска кампании: max_groups из формы + режим «только парсинг»."""
        try:
            limit = int(self.limit.get().strip() or 0)
        except ValueError:
            limit = 0
        params = []
        if limit > 0:
            params.append(f"max_groups={limit}")
        if parse_only:
            params.append("parse_only=true")
        path = f"/campaigns/{campaign_id}/start"
        return path + ("?" + "&".join(params) if params else "")

    def on_restart(self) -> None:
        # «Запустить только парсер»: Orchestrator делает парсинг и останавливается, даже если
        # у кампании уже есть шаблон. Найденные лиды остаются status=new для отдельной рассылки.
        campaign_id = self._require_selected()
        if not campaign_id:
            return
        path = self._start_path(campaign_id, parse_only=True)

        def start():
            return self.api(self.orchestrator_url, path, method="POST")

        self.app.run_bg(
            start,
            on_done=lambda _r: self.app.flash_status(
                "Запущен только парсинг — найденные организации останутся неотправленными"
            ),
            on_error=lambda exc: messagebox.showerror(self.source.title, str(exc)),
        )

    def on_start_all(self) -> None:
        # «Запустить всё вместе»: сначала парсинг новых организаций, затем рассылка по всем
        # лидам кампании со status=new (не только по найденным в этом прогоне — как обычный
        # запуск кампании в VK-вкладке). Видимый шаблон сохраняем перед стартом, иначе
        # Orchestrator не увидит его и завершится после парсинга.
        campaign_id = self._require_selected()
        if not campaign_id:
            return
        if self._send_campaign_id is not None or self._followup_campaign_id is not None:
            messagebox.showinfo("Запуск", "Рассылка уже идёт, дождитесь её окончания")
            return
        path = self._start_path(campaign_id, parse_only=False)
        body = self.message.get("1.0", "end").strip()
        variant = self.tpl_variant.get()

        def start():
            if body and variant != FOLLOWUP_VARIANT:
                self.api(
                    self.data_url, "/templates", method="POST",
                    body={"campaign_id": campaign_id, "variant": variant, "body": body},
                )
            return self.api(self.orchestrator_url, path, method="POST")

        self.app.run_bg(
            start,
            on_done=lambda _r: self.app.flash_status(
                "Запущены парсинг и рассылка — уже найденные организации будут пропущены при поиске"
            ),
            on_error=lambda exc: messagebox.showerror(self.source.title, str(exc)),
        )

    def on_send(self, retry_failed: bool = False) -> None:
        # Только рассылка уже найденным (status=new), без нового поиска — напрямую в Messaging
        # Service, как кнопки «Запустить рассылку» / «Повторить с ошибками» на основной вкладке.
        campaign_id = self._require_selected()
        if not campaign_id:
            return
        if self._send_campaign_id is not None or self._followup_campaign_id is not None:
            messagebox.showinfo("Рассылка", "Рассылка уже идёт, дождитесь её окончания")
            return
        self._send_campaign_id = campaign_id
        body = self.message.get("1.0", "end").strip()
        variant = self.tpl_variant.get()

        def start():
            # Текст из редактора — тот, что пользователь видит перед нажатием; сохраняем его
            # (upsert по campaign_id+variant), иначе у кампании «Только найти» шаблона нет вовсе.
            # F для первой рассылки не сохраняем: это шаблон повторного сообщения.
            if body and variant != FOLLOWUP_VARIANT:
                self.api(
                    self.data_url, "/templates", method="POST",
                    body={"campaign_id": campaign_id, "variant": variant, "body": body},
                )
            path = f"/campaigns/{campaign_id}/send" + ("?retry_failed=true" if retry_failed else "")
            return self.api(self.messaging_url, path, method="POST")

        def error(exc):
            self._send_campaign_id = None
            messagebox.showerror("Рассылка", str(exc))

        self.app.run_bg(start, on_done=lambda _r: self.after(500, self._poll_send), on_error=error)

    def on_stop_send(self) -> None:
        """Останавливает текущую рассылку: Messaging Service завершит текущего лида и дальше
        не пойдёт. Если рассылки нет — сервис вернёт 404, покажем это без падения."""
        campaign_id = self._require_selected()
        if not campaign_id:
            return
        self.app.flash_status("Останавливаю рассылку…")

        def stop():
            return self.api(self.messaging_url, f"/campaigns/{campaign_id}/stop", method="POST")

        def done(result):
            status = (result or {}).get("status")
            if status == "stopped":
                self._send_campaign_id = None
                self.app.flash_status("Рассылка остановлена")
                self.refresh_detail()
                return
            # Задача сейчас в середине лида/паузы — дождёмся, пока она сама увидит флаг.
            self._send_campaign_id = campaign_id
            self.after(500, self._poll_send)

        def error(exc):
            self._send_campaign_id = None
            messagebox.showinfo(self.source.title, f"Не удалось остановить рассылку: {exc}")

        self.app.run_bg(stop, on_done=done, on_error=error)

    def _poll_send(self) -> None:
        campaign_id = self._send_campaign_id
        if campaign_id is None:
            return

        def fetch():
            return self.api(self.messaging_url, f"/campaigns/{campaign_id}/send-status", timeout=15.0)

        def done(result):
            counts = f"отправлено {result['sent']}, ошибок {result['failed']}"
            status = result["status"]
            if status in ("queued", "running"):
                if campaign_id == self.selected_id:
                    self.status_label.configure(text=f"Рассылка: {counts}…", foreground="#555")
                self.after(2000, self._poll_send)
                return
            self._send_campaign_id = None
            if status == "stopped":
                self.app.flash_status(f"Рассылка остановлена ({counts})")
                self.refresh_detail()
                return
            if status == "failed":
                messagebox.showerror("Рассылка", result.get("error") or "не удалось запустить рассылку")
            elif status == "waiting_for_account":
                # Раньше это был только текст в нижней статус-строке — легко не заметить и решить,
                # что кнопка вообще ничего не делает. Теперь показываем диалог с причиной
                # (например, протухшей сессией WhatsApp) и что именно нажать.
                reason = result.get("error")
                self.app.flash_status(f"Рассылка приостановлена: нет свободного аккаунта ({counts})", is_err=True)
                messagebox.showwarning(
                    "Рассылка",
                    f"Рассылка остановлена: нет свободного аккаунта ({counts})."
                    + (f"\n\nПричина: {reason}" if reason else "")
                    + "\n\nПроверьте панель аккаунтов: нужен аккаунт нужной платформы со входом "
                    "и без паузы. Для WhatsApp нажмите «Войти в WhatsApp», после входа — "
                    "«Продолжить», чтобы снять паузу, и запустите рассылку снова.",
                )
            else:
                self.app.flash_status(f"Рассылка завершена: {counts}")
            self.refresh_detail()

        def error(exc):
            self._send_campaign_id = None
            messagebox.showerror("Рассылка", str(exc))

        self.app.run_bg(fetch, on_done=done, on_error=error, dedupe_key=f"{self.source.platform}_send_status")

    def on_send_followups(self) -> None:
        # Одно повторное сообщение (шаблон F) лидам, которым писали больше FOLLOWUP_DELAY_DAYS
        # назад и которые так и не ответили — см. messaging-service/app/followup.py.
        campaign_id = self._require_selected()
        if not campaign_id:
            return
        if self._send_campaign_id is not None or self._followup_campaign_id is not None:
            messagebox.showinfo("Повторные сообщения", "Отправка уже идёт, дождитесь её окончания")
            return
        body = self.message.get("1.0", "end").strip()
        variant = self.tpl_variant.get()
        if "F" not in self._templates and not (variant == FOLLOWUP_VARIANT and body):
            messagebox.showwarning(
                "Повторные сообщения",
                "Сначала выберите вариант F, введите текст повторного сообщения и нажмите "
                "«Сохранить шаблон».",
            )
            return
        self._followup_campaign_id = campaign_id
        self.status_label.configure(text="Повторные: ищу неответивших…", foreground="#555")

        def start():
            # Если открыт F — сохраняем видимый текст перед отправкой (как «Запустить рассылку»
            # сохраняет A/B): у кампании «Только найти» шаблона F иначе может не быть вовсе.
            if variant == FOLLOWUP_VARIANT and body:
                self.api(
                    self.data_url, "/templates", method="POST",
                    body={"campaign_id": campaign_id, "variant": FOLLOWUP_VARIANT, "body": body},
                )
            return self.api(self.messaging_url, f"/campaigns/{campaign_id}/followup", method="POST")

        def error(exc):
            self._followup_campaign_id = None
            messagebox.showerror("Повторные сообщения", str(exc))

        self.app.run_bg(start, on_done=lambda _r: self.after(500, self._poll_followup), on_error=error)

    def _poll_followup(self) -> None:
        campaign_id = self._followup_campaign_id
        if campaign_id is None:
            return

        def fetch():
            return self.api(self.messaging_url, f"/campaigns/{campaign_id}/followup-status", timeout=15.0)

        def done(result):
            counts = f"отправлено {result['sent']}, ошибок {result['failed']}, пропущено {result['skipped']}"
            status = result["status"]
            if status in ("queued", "running"):
                if campaign_id == self.selected_id:
                    self.status_label.configure(text=f"Повторные: {counts}…", foreground="#555")
                self.after(2000, self._poll_followup)
                return
            self._followup_campaign_id = None
            if status == "failed":
                messagebox.showerror("Повторные сообщения", result.get("error") or "не удалось отправить повторные")
            elif status == "waiting_for_account":
                self.app.flash_status(f"Повторные приостановлены: нет свободного аккаунта ({counts})", is_err=True)
            else:
                self.app.flash_status(f"Повторные отправлены: {counts}")
            self.refresh_detail()

        def error(exc):
            self._followup_campaign_id = None
            messagebox.showerror("Повторные сообщения", str(exc))

        self.app.run_bg(fetch, on_done=done, on_error=error, dedupe_key=f"{self.source.platform}_followup_status")

    def on_refresh(self) -> None:
        self.refresh_campaigns()
        self.refresh_detail()

    # ---------- шаблоны ----------
    def _on_variant_selected(self, _event=None) -> None:
        variant = self.tpl_variant.get()
        template = self._templates.get(variant)
        self._set_template_editor(template["body"] if template else (DEFAULT_MESSAGE if variant == "A" else ""))

    def _on_template_row_selected(self, _event=None) -> None:
        selected = self.tpl_tree.selection()
        if not selected:
            return
        variant, body = self.tpl_tree.item(selected[0], "values")
        self.tpl_variant.set(variant)
        self._set_template_editor(body)

    def _set_template_editor(self, body: str) -> None:
        self.message.delete("1.0", "end")
        if body:
            self.message.insert("1.0", body)

    def _render_templates(self, templates: list[dict]) -> None:
        selected = self.tpl_tree.selection()
        own = [t for t in templates if t.get("campaign_id") == self.selected_id]
        self._templates = {t["variant"]: t for t in own}
        self.tpl_tree.delete(*self.tpl_tree.get_children())
        for t in sorted(own, key=lambda t: t["variant"]):
            self.tpl_tree.insert("", "end", iid=t["id"], values=(t["variant"], t["body"]))
        still = [i for i in selected if self.tpl_tree.exists(i)]
        if still:
            self.tpl_tree.selection_set(still)

    def refresh_templates(self) -> None:
        campaign_id = self.selected_id
        if not campaign_id:
            return

        def fetch():
            return self.api(self.data_url, "/templates")

        def done(templates):
            if campaign_id == self.selected_id:
                self._render_templates(templates)

        self.app.run_bg(fetch, on_done=done, dedupe_key=f"{self.source.platform}_templates")

    def on_save_template(self) -> None:
        campaign_id = self._require_selected()
        if not campaign_id:
            return
        body = self.message.get("1.0", "end").strip()
        if not body:
            messagebox.showwarning("Шаблон", "Введите текст сообщения слева")
            return
        variant = self.tpl_variant.get()

        def save():
            # data-service делает upsert по (campaign_id, variant) — повторное сохранение
            # обновляет существующий вариант, а не создаёт дубликат.
            return self.api(
                self.data_url, "/templates", method="POST",
                body={"campaign_id": campaign_id, "variant": variant, "body": body},
            )

        def done(_r):
            self.app.flash_status(f"Шаблон {variant} сохранён для выбранной кампании")
            self.refresh_templates()

        self.app.run_bg(save, on_done=done, on_error=lambda exc: messagebox.showerror("Шаблон", str(exc)))

    def on_delete_template(self) -> None:
        selected = self.tpl_tree.selection()
        if selected:
            template_id = selected[0]
            variant, body = self.tpl_tree.item(template_id, "values")
        else:
            # Строка не выбрана — удаляем вариант, который сейчас открыт в редакторе.
            template = self._templates.get(self.tpl_variant.get())
            if template is None:
                messagebox.showinfo("Шаблон", "Сначала выберите шаблон в таблице справа")
                return
            template_id, variant, body = template["id"], template["variant"], template["body"]
        if not messagebox.askyesno("Удалить шаблон", f'Удалить вариант {variant} — "{body}"?'):
            return

        def delete():
            return self.api(self.data_url, f"/templates/{template_id}", method="DELETE")

        def done(_r):
            self.app.flash_status(f"Шаблон {variant} удалён")
            self.refresh_templates()

        self.app.run_bg(delete, on_done=done, on_error=lambda exc: messagebox.showerror("Ошибка", str(exc)))

    def on_delete(self) -> None:
        campaign_id = self._require_selected()
        if not campaign_id:
            return
        name = self._campaigns.get(campaign_id, {}).get("name", campaign_id)
        if not messagebox.askyesno(
            "Удалить кампанию",
            f"Удалить кампанию «{name}»?\n\nНайденные организации останутся в базе (им повторно не напишем).",
        ):
            return

        def delete():
            return self.api(self.data_url, f"/campaigns/{campaign_id}", method="DELETE")

        def done(_r):
            self.selected_id = None
            self.leads_tree.delete(*self.leads_tree.get_children())
            self.tpl_tree.delete(*self.tpl_tree.get_children())
            self._templates = {}
            self.status_label.configure(text="")
            self.stats_label.configure(text="")
            self.refresh_campaigns()

        self.app.run_bg(delete, on_done=done, on_error=lambda exc: messagebox.showerror("Ошибка", str(exc)))

    def _require_selected(self) -> str | None:
        if not self.selected_id:
            messagebox.showinfo(self.source.title, "Сначала выберите кампанию в списке")
        return self.selected_id

    # ---------- обновление ----------
    def _tick(self) -> None:
        self._refresh_accounts_hint()
        if self.winfo_ismapped():
            self.refresh_campaigns()
            self.refresh_detail()
        self.after(REFRESH_MS, self._tick)

    def refresh_campaigns(self) -> None:
        def fetch():
            return self.api(self.data_url, "/campaigns")

        def done(campaigns):
            own = [c for c in campaigns if c.get("platform") == self.source.platform]
            self._campaigns = {c["id"]: c for c in own}
            self.camp_tree.delete(*self.camp_tree.get_children())
            for c in sorted(own, key=lambda c: c.get("created_at", ""), reverse=True):
                params = c.get("parse_params") or {}
                channels = " → ".join(CHANNEL_LABELS.get(ch, ch) for ch in params.get("channels") or [])
                place = _place_label(params.get("city") or "", params.get("country") or "")
                self.camp_tree.insert(
                    "", "end", iid=c["id"],
                    values=(c["name"], place, c["keyword"], channels,
                            RU_CAMPAIGN_STATUS.get(c["status"], c["status"])),
                )
            if self.selected_id and self.camp_tree.exists(self.selected_id):
                self.camp_tree.selection_set(self.selected_id)

        self.app.run_bg(fetch, on_done=done, dedupe_key=f"{self.source.platform}_campaigns")

    def _on_select(self, _event=None) -> None:
        selected = self.camp_tree.selection()
        if not selected or selected[0] == self.selected_id:
            return
        self.selected_id = selected[0]
        self.leads_tree.delete(*self.leads_tree.get_children())
        # Подставляем в редактор вариант A выбранной кампании (если он есть); дальше
        # пользователь может переключить вариант в списке слева.
        self.refresh_detail(load_template=True)

    def refresh_detail(self, load_template: bool = False) -> None:
        campaign_id = self.selected_id
        if not campaign_id:
            return

        def fetch():
            status = self.api(self.orchestrator_url, f"/campaigns/{campaign_id}")
            leads = self.api(self.data_url, f"/leads?campaign_id={campaign_id}&limit=2000")
            templates = self.api(self.data_url, "/templates")
            return status, leads, templates

        def done(result):
            if campaign_id != self.selected_id:
                return
            status, leads, templates = result
            self._render_status(status)
            self._render_leads(leads)
            self._render_templates(templates)
            if load_template:
                self.tpl_variant.set("A")
                self._on_variant_selected()

        self.app.run_bg(fetch, on_done=done, dedupe_key=f"{self.source.platform}_detail")

    def _render_status(self, data: dict) -> None:
        phase = data.get("phase")
        text = f"Фаза: {RU_PHASE.get(phase, phase)}"
        progress = data.get("progress") or {}
        if phase == "parsing":
            text += (
                f" · просмотрено карточек {progress.get('checked', 0)}/{progress.get('total', 0)},"
                f" подходит: {progress.get('found', 0)}"
            )
        elif phase == "messaging":
            text += (
                f" · отправлено {progress.get('sent', 0)}, ошибок {progress.get('failed', 0)},"
                f" пропущено {progress.get('skipped', 0)}"
            )
        if data.get("error"):
            text += f" · ошибка: {data['error']}"
        elif data.get("note"):
            text += f" · {data['note']}"
        if self._send_campaign_id != self.selected_id and self._followup_campaign_id != self.selected_id:
            self.status_label.configure(text=text, foreground="#c0392b" if data.get("error") else "#555")
        stats = data.get("stats") or {}
        self.stats_label.configure(
            text="   ".join(f"{RU_LEAD_STATUS.get(k, k)}: {v}" for k, v in stats.items() if k != "queued")
        )

    def _render_leads(self, leads: list[dict]) -> None:
        selected = self.leads_tree.selection()
        self.leads_tree.delete(*self.leads_tree.get_children())
        for lead in leads:
            self.leads_tree.insert(
                "", "end", iid=lead["id"],
                values=(
                    lead.get("title") or "",
                    CHANNEL_LABELS.get(lead["platform"], lead["platform"]),
                    lead.get("group_url") or "",
                    lead.get("admin_contact") or "",
                    RU_LEAD_STATUS.get(lead["status"], lead["status"]),
                ),
            )
        still = [i for i in selected if self.leads_tree.exists(i)]
        if still:
            self.leads_tree.selection_set(still)

    def _is_info_column(self, x: int) -> bool:
        """Колонка «Телефон / адрес» по имени, а не по жёсткому номеру #4."""
        try:
            index = list(self.leads_tree["columns"]).index("info") + 1
        except ValueError:
            return False
        return self.leads_tree.identify_column(x) == f"#{index}"

    def _lead_row_at(self, event) -> str | None:
        if self.leads_tree.identify_region(event.x, event.y) != "cell":
            return None
        return self.leads_tree.identify_row(event.y) or None

    def _lead_map_url(self, iid: str) -> str | None:
        contact = self.leads_tree.item(iid, "values")[3]
        # Сначала точная карточка организации из парсера, иначе — поиск карт по адресу.
        return _map_url_from_contact(contact) or _map_search_url(
            _address_from_contact(contact), self.source
        )

    def _on_lead_click(self, event) -> None:
        """Одиночный клик по колонке «Телефон / адрес» открывает заведение на карте."""
        if not self._is_info_column(event.x):
            return
        iid = self._lead_row_at(event)
        if not iid:
            return
        url = self._lead_map_url(iid)
        if not url:
            return
        now = time.monotonic()
        # Двойной клик в Tk — это два <Button-1>; вторую вкладку того же заведения не открываем.
        if self._last_map_open[0] == url and now - self._last_map_open[1] < 1.0:
            return
        self._last_map_open = (url, now)
        webbrowser.open(url)

    def _on_lead_motion(self, event) -> None:
        over_map_cell = (
            self.leads_tree.identify_region(event.x, event.y) == "cell"
            and self._is_info_column(event.x)
            and bool(self.leads_tree.identify_row(event.y))
        )
        self.leads_tree.configure(cursor="hand2" if over_map_cell else "")

    def _on_lead_double_click(self, event=None) -> None:
        # По колонке адреса одиночный клик уже открыл карту — контакт поверх неё не открываем.
        if event is not None and self._is_info_column(event.x):
            return
        selected = self.leads_tree.selection()
        if not selected:
            return
        url = self.leads_tree.item(selected[0], "values")[2]
        if url:
            webbrowser.open(url)
