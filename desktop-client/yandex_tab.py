"""Вкладки «Яндекс карты» и «Google карты»: поиск организаций по городу и виду деятельности + рассылка
по тому каналу, который указан в карточке организации (Яндекс — VK / Telegram / WhatsApp,
Google — Facebook / Instagram / Telegram, с поиском заведения в соцсетях; см. MapSource).

Отдельный модуль, а не ещё пара сотен строк в app.py. Сам ничего не импортирует из app.py
(тот запускается как __main__ — повторный импорт выполнил бы его второй раз): всё нужное
(run_bg, flash_status, кэш аккаунтов, api_request и адреса сервисов) приходит через App.

Как это устроено на бэкенде:
  - кампания с platform="yandex_maps" (или "google_maps"), keyword = вид деятельности, parse_params =
    {"city", "channels", "has_site", "whatsapp_by_phone"} (Data Service, миграция 0008);
  - parser-service открывает Яндекс/Google Карты, обходит карточки и пишет по одному лиду на
    организацию с платформой канала (vk/tg/whatsapp/facebook) — первый по приоритету, что есть в карточке;
  - messaging-service рассылает как обычно, беря аккаунт по платформе лида.
"""

import itertools
import tkinter as tk
import webbrowser
from dataclasses import dataclass
from tkinter import messagebox, ttk

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


YANDEX = MapSource(
    "yandex_maps", "Яндекс карты", "по Яндекс Картам", "Яндекс Карт", "на Яндекс Картах",
    ("vk", "tg", "whatsapp"), phone_channels=("whatsapp",),
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

REFRESH_MS = 3000
# Аккаунт пригоден для рассылки, если у него есть сохранённая сессия, назначение
# messaging/both и он не забанен/не на паузе.
_USABLE_ACCOUNT_STATUSES = {"active", "locked"}


def _priority_orders(channels: list[str]) -> list[tuple[str, ...]]:
    return list(itertools.permutations(channels))


def _order_label(order: tuple[str, ...]) -> str:
    return " → ".join(CHANNEL_LABELS[c] for c in order)


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
        # True, пока пользователь сам не трогал галочки каналов — до этого они следуют за
        # наличием аккаунтов ("смотря что есть"), потом — его выбор не перетираем.
        self._channels_auto = True

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

        r = next(row)
        ttk.Label(panel, text="Город:").grid(row=r, column=0, sticky="w", pady=3)
        self.city = ttk.Combobox(panel, values=CITIES, width=28)
        self.city.grid(row=r, column=1, sticky="ew", pady=3)

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
        self.wa_by_phone = tk.BooleanVar(value=False)
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
        ttk.Label(panel, text="ТЕКСТ СООБЩЕНИЯ", font=("Segoe UI", 9, "bold"), foreground="#777").grid(
            row=r, column=0, columnspan=2, sticky="w", pady=(10, 2)
        )
        r = next(row)
        self.message = tk.Text(panel, height=6, width=40, wrap="word", font=("Segoe UI", 9))
        self.message.grid(row=r, column=0, columnspan=2, sticky="ew")
        self.message.insert("1.0", "Здравствуйте, {{org_name}}!")
        ttk.Label(
            panel, text="{{org_name}} — название организации", foreground="#999", font=("Segoe UI", 8)
        ).grid(row=next(row), column=0, columnspan=2, sticky="w")

        r = next(row)
        btns = ttk.Frame(panel)
        btns.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Button(btns, text="Найти и разослать", style="Accent.TButton", command=lambda: self.on_create(True)).pack(
            side="left"
        )
        ttk.Button(btns, text="Только найти", command=lambda: self.on_create(False)).pack(side="left", padx=(6, 0))

        self._refresh_priority_choices()
        return panel

    def _build_right(self) -> ttk.Frame:
        frame = ttk.Frame(self)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(3, weight=1)

        camp_box = ttk.LabelFrame(frame, text=f"Кампании {self.source.of}", padding=8)
        camp_box.grid(row=0, column=0, sticky="ew")
        cols = ("name", "city", "activity", "channels", "status")
        self.camp_tree = ttk.Treeview(camp_box, columns=cols, show="headings", height=6, selectmode="browse")
        for col, title, width in (
            ("name", "Название", 200),
            ("city", "Город", 110),
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
        ttk.Button(actions, text="Искать ещё", command=self.on_restart).pack(side="left")
        ttk.Button(actions, text="Запустить рассылку", command=self.on_send).pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="Сохранить текст сообщения", command=self.on_save_template).pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="Удалить", style="Danger.TButton", command=self.on_delete).pack(side="left", padx=(6, 0))

        self.status_label = ttk.Label(frame, text="Выберите кампанию или создайте новую слева", foreground="#555")
        self.status_label.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.stats_label = ttk.Label(frame, text="", font=("Segoe UI", 10))
        self.stats_label.grid(row=2, column=0, sticky="ew", pady=(2, 6))

        leads_box = ttk.LabelFrame(frame, text="Найденные организации (двойной клик — открыть контакт)", padding=8)
        leads_box.grid(row=3, column=0, sticky="nsew")
        cols = ("title", "channel", "url", "info", "status")
        self.leads_tree = ttk.Treeview(leads_box, columns=cols, show="headings")
        for col, title, width in (
            ("title", "Организация", 180),
            ("channel", "Канал", 75),
            ("url", "Контакт", 190),
            ("info", "Телефон / адрес", 300),
            ("status", "Статус", 90),
        ):
            self.leads_tree.heading(col, text=title)
            self.leads_tree.column(col, width=width, anchor="w")
        scroll = ttk.Scrollbar(leads_box, orient="vertical", command=self.leads_tree.yview)
        self.leads_tree.configure(yscrollcommand=scroll.set)
        self.leads_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.leads_tree.bind("<Double-1>", self._on_lead_double_click)
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

    # ---------- создание / запуск ----------
    def on_create(self, with_send: bool) -> None:
        city = self.city.get().strip()
        activity = self.activity.get().strip()
        if not city or not activity:
            messagebox.showwarning(self.source.title, "Укажите город и вид деятельности")
            return
        order = self._priority_map.get(self.priority.get())
        if not order:
            messagebox.showwarning(self.source.title, "Выберите хотя бы один канал рассылки")
            return
        try:
            limit = int(self.limit.get().strip() or 0)
        except ValueError:
            messagebox.showwarning(self.source.title, "«Найти организаций» должно быть числом")
            return
        body = self.message.get("1.0", "end").strip()
        if with_send and not body:
            messagebox.showwarning(self.source.title, "Введите текст сообщения")
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

        name = self.name.get().strip() or f"{activity} — {city}"
        payload = {
            "name": name,
            "platform": self.source.platform,
            "keyword": activity,
            "use_account": False,
            "parse_params": {
                "city": city,
                "channels": list(order),
                "has_site": False if self.only_without_site.get() else None,
                "whatsapp_by_phone": self.wa_by_phone.get(),
                "search_socials": self.search_socials.get(),
            },
        }

        def create_and_start():
            campaign = self.api(self.orchestrator_url, "/campaigns", method="POST", body=payload)
            if with_send:
                self.api(
                    self.data_url, "/templates", method="POST",
                    body={"campaign_id": campaign["id"], "variant": "A", "body": body},
                )
            # Без шаблона оркестратор сам завершит кампанию после парсинга ("Только найти").
            path = f"/campaigns/{campaign['id']}/start"
            if limit > 0:
                path += f"?max_groups={limit}"
            self.api(self.orchestrator_url, path, method="POST")
            return campaign

        def done(campaign):
            self.app.flash_status(f"Кампания «{campaign['name']}» запущена: ищу {self.source.on}…")
            self.name.delete(0, "end")
            self.selected_id = campaign["id"]
            self.refresh_campaigns()

        def error(exc):
            messagebox.showerror(self.source.title, str(exc))

        self.app.run_bg(create_and_start, on_done=done, on_error=error)

    def on_restart(self) -> None:
        campaign_id = self._require_selected()
        if not campaign_id:
            return
        try:
            limit = int(self.limit.get().strip() or 0)
        except ValueError:
            limit = 0
        path = f"/campaigns/{campaign_id}/start" + (f"?max_groups={limit}" if limit > 0 else "")

        def start():
            return self.api(self.orchestrator_url, path, method="POST")

        self.app.run_bg(
            start,
            on_done=lambda _r: self.app.flash_status("Поиск запущен — уже найденные организации будут пропущены"),
            on_error=lambda exc: messagebox.showerror(self.source.title, str(exc)),
        )

    def on_send(self) -> None:
        # Только рассылка уже найденным (status=new), без нового поиска — напрямую в Messaging
        # Service, как кнопка «Запустить рассылку» на основной вкладке.
        campaign_id = self._require_selected()
        if not campaign_id:
            return
        if self._send_campaign_id is not None:
            messagebox.showinfo("Рассылка", "Рассылка уже идёт, дождитесь её окончания")
            return
        self._send_campaign_id = campaign_id
        body = self.message.get("1.0", "end").strip()

        def start():
            # Текст из поля слева — тот, что пользователь видит перед нажатием; сохраняем его
            # (upsert по campaign_id+variant), иначе у кампании «Только найти» шаблона нет вовсе.
            if body:
                self.api(
                    self.data_url, "/templates", method="POST",
                    body={"campaign_id": campaign_id, "variant": "A", "body": body},
                )
            return self.api(self.messaging_url, f"/campaigns/{campaign_id}/send", method="POST")

        def error(exc):
            self._send_campaign_id = None
            messagebox.showerror("Рассылка", str(exc))

        self.app.run_bg(start, on_done=lambda _r: self.after(500, self._poll_send), on_error=error)

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
            if status == "failed":
                messagebox.showerror("Рассылка", result.get("error") or "не удалось запустить рассылку")
            elif status == "waiting_for_account":
                self.app.flash_status(f"Рассылка приостановлена: нет свободного аккаунта ({counts})", is_err=True)
            else:
                self.app.flash_status(f"Рассылка завершена: {counts}")

        def error(exc):
            self._send_campaign_id = None
            messagebox.showerror("Рассылка", str(exc))

        self.app.run_bg(fetch, on_done=done, on_error=error, dedupe_key=f"{self.source.platform}_send_status")

    def on_save_template(self) -> None:
        campaign_id = self._require_selected()
        if not campaign_id:
            return
        body = self.message.get("1.0", "end").strip()
        if not body:
            messagebox.showwarning("Шаблон", "Введите текст сообщения слева")
            return

        def save():
            return self.api(
                self.data_url, "/templates", method="POST", body={"campaign_id": campaign_id, "variant": "A", "body": body}
            )

        self.app.run_bg(
            save,
            on_done=lambda _r: self.app.flash_status("Текст сообщения сохранён для выбранной кампании"),
            on_error=lambda exc: messagebox.showerror("Шаблон", str(exc)),
        )

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
                self.camp_tree.insert(
                    "", "end", iid=c["id"],
                    values=(c["name"], params.get("city", ""), c["keyword"], channels,
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
        self._load_template(self.selected_id)
        self.refresh_detail()

    def _load_template(self, campaign_id: str) -> None:
        def fetch():
            return self.api(self.data_url, "/templates")

        def done(templates):
            own = [t for t in templates if t.get("campaign_id") == campaign_id and t.get("variant") == "A"]
            if own and campaign_id == self.selected_id:
                self.message.delete("1.0", "end")
                self.message.insert("1.0", own[0]["body"])

        self.app.run_bg(fetch, on_done=done)

    def refresh_detail(self) -> None:
        campaign_id = self.selected_id
        if not campaign_id:
            return

        def fetch():
            status = self.api(self.orchestrator_url, f"/campaigns/{campaign_id}")
            leads = self.api(self.data_url, f"/leads?campaign_id={campaign_id}&limit=2000")
            return status, leads

        def done(result):
            if campaign_id != self.selected_id:
                return
            status, leads = result
            self._render_status(status)
            self._render_leads(leads)

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
        if self._send_campaign_id != self.selected_id:
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

    def _on_lead_double_click(self, _event=None) -> None:
        selected = self.leads_tree.selection()
        if not selected:
            return
        url = self.leads_tree.item(selected[0], "values")[2]
        if url:
            webbrowser.open(url)
