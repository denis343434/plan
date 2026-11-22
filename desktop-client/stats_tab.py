"""Вкладка «Статистика»: сколько отправлено и сколько ответили — по шаблонам, кампаниям, дням.

Сама ничего не считает — берёт готовые агрегаты у Data Service (GET /stats/messages, см.
data-service/app/crud/stats.py, там же — как ответ привязывается к первому сообщению или к
повтору). Как и yandex_tab.py, ничего не импортирует из app.py: run_bg/flash_status и
api_request приходят через App.
"""

import csv
import time
from datetime import date, datetime, timedelta
from tkinter import filedialog, ttk
from urllib.parse import urlencode

GROUPINGS = {
    "По шаблонам": "variant",
    "По кампаниям": "campaign",
    "По дням": "day",
    "По платформам": "platform",
}
PERIODS = ("Сегодня", "7 дней", "30 дней", "Всё время")
REPLY_WINDOWS = ("1", "3", "6", "12", "24", "48", "72", "168")
PLATFORM_LABELS = {
    "vk": "VK", "tg": "Telegram", "instagram": "Instagram", "whatsapp": "WhatsApp", "facebook": "Facebook",
}
ALL = "Все"
DATE_FMT = "%d.%m.%Y"

# Колонки ключа по группировке: (id, заголовок, ширина).
_KEY_COLUMNS = {
    "variant": [("campaign", "Кампания", 200), ("variant", "Сообщение", 110)],
    "campaign": [("campaign", "Кампания", 260)],
    "day": [("day", "День отправки", 130)],
    "platform": [("platform", "Платформа", 130)],
}


def _local_tz_name() -> str:
    """Пояс для группировки "по дням" — фиксированное смещение этой машины (в России нет
    перехода на летнее время). В Etc/GMT знак инвертирован: UTC+3 — это Etc/GMT-3."""
    offset = time.localtime().tm_gmtoff
    if offset % 3600:
        return "Europe/Moscow"
    hours = offset // 3600
    return "UTC" if hours == 0 else f"Etc/GMT{-hours:+d}"


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _hours(value: float | None) -> str:
    if value is None:
        return "—"
    if value < 1:
        return f"{round(value * 60)} мин"
    if value < 48:
        return f"{value:.1f} ч"
    return f"{value / 24:.1f} дн"


class StatsTab(ttk.Frame):
    def __init__(self, parent, app, api_request, data_url: str) -> None:
        super().__init__(parent, padding=10)
        self.app = app
        self.api = api_request
        self.data_url = data_url
        self.tz = _local_tz_name()

        # имя в комбобоксе -> campaign_id
        self._campaign_ids: dict[str, str] = {}
        self._last_rows: list[dict] = []
        self._last_group = "variant"

        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        self._build_filters().grid(row=0, column=0, sticky="ew")
        self._build_summary().grid(row=1, column=0, sticky="ew", pady=(8, 8))
        self._build_table().grid(row=2, column=0, sticky="nsew")
        ttk.Label(
            self,
            text=(
                "Ответ считается по времени, когда его нашла «Проверить ответы», — если проверять "
                "редко, «ответили за N ч» будет занижено. Ответ лида засчитывается последнему "
                "сообщению, ушедшему до него (первому или повтору), ручные ответы не учитываются."
            ),
            foreground="#777", wraplength=1050, justify="left",
        ).grid(row=3, column=0, sticky="w", pady=(6, 0))

        self._set_period("30 дней")
        # ttk.Notebook прячет неактивные вкладки через unmap — <Map> срабатывает при каждом
        # переключении на вкладку, обновляем данные именно тогда, а не фоновым таймером.
        self.bind("<Map>", lambda _e: self.refresh())

    # ---------- layout ----------
    def _build_filters(self) -> ttk.Frame:
        bar = ttk.Frame(self)

        ttk.Label(bar, text="Кампания:").pack(side="left")
        self.campaign = ttk.Combobox(bar, state="readonly", width=28, values=[ALL])
        self.campaign.set(ALL)
        self.campaign.pack(side="left", padx=(4, 12))

        ttk.Label(bar, text="Платформа:").pack(side="left")
        self.platform = ttk.Combobox(
            bar, state="readonly", width=11, values=[ALL, *PLATFORM_LABELS.values()]
        )
        self.platform.set(ALL)
        self.platform.pack(side="left", padx=(4, 12))

        ttk.Label(bar, text="с").pack(side="left")
        self.date_from = ttk.Entry(bar, width=11)
        self.date_from.pack(side="left", padx=4)
        ttk.Label(bar, text="по").pack(side="left")
        self.date_to = ttk.Entry(bar, width=11)
        self.date_to.pack(side="left", padx=(4, 4))
        self.period = ttk.Combobox(bar, state="readonly", width=10, values=PERIODS)
        self.period.pack(side="left", padx=(0, 12))
        self.period.bind("<<ComboboxSelected>>", lambda _e: (self._set_period(self.period.get()), self.refresh()))

        ttk.Label(bar, text="Ответ за, ч:").pack(side="left")
        self.window = ttk.Combobox(bar, state="readonly", width=5, values=REPLY_WINDOWS)
        self.window.set("24")
        self.window.pack(side="left", padx=(4, 12))

        self.group = ttk.Combobox(bar, state="readonly", width=14, values=list(GROUPINGS))
        self.group.set("По шаблонам")
        self.group.pack(side="left", padx=(0, 12))

        for combo in (self.campaign, self.platform, self.window, self.group):
            combo.bind("<<ComboboxSelected>>", lambda _e: self.refresh(), add="+")
        for entry in (self.date_from, self.date_to):
            entry.bind("<Return>", lambda _e: self.refresh())
            # Даты поправили руками — это уже не "7 дней"/"30 дней" из списка.
            entry.bind("<Key>", lambda _e: self.period.set(""), add="+")

        ttk.Button(bar, text="Обновить", style="Accent.TButton", command=self.refresh).pack(side="left")
        ttk.Button(bar, text="Экспорт CSV", command=self.on_export).pack(side="left", padx=(6, 0))
        return bar

    def _build_summary(self) -> ttk.Frame:
        frame = ttk.Frame(self)
        self.summary_labels: dict[str, ttk.Label] = {}
        for key, title in (
            ("sent", "Отправлено"),
            ("replied", "Ответили"),
            ("window", "Ответили за 24 ч"),
            ("median", "Медиана ответа"),
            ("failed", "Ошибки / не ушло"),
        ):
            box = ttk.Frame(frame, padding=(0, 0, 28, 0))
            box.pack(side="left")
            title_lbl = ttk.Label(box, text=title, foreground="#777")
            title_lbl.pack(anchor="w")
            value = ttk.Label(box, text="—", font=("Segoe UI", 14, "bold"))
            value.pack(anchor="w")
            self.summary_labels[key] = value
            if key == "window":
                self._window_title = title_lbl
        return frame

    def _build_table(self) -> ttk.Frame:
        frame = ttk.Frame(self)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(frame, show="headings")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        return frame

    def _metric_columns(self) -> list[tuple[str, str, int]]:
        hours = self.window.get()
        return [
            ("sent", "Отправлено", 90),
            ("replied", "Ответили", 80),
            ("rate", "% ответов", 80),
            ("window", f"За {hours} ч", 80),
            ("window_rate", f"% за {hours} ч", 85),
            ("median", "Медиана ответа", 110),
            ("failed", "Ошибки", 70),
            ("pending", "Не ушло", 70),
        ]

    # ---------- filters ----------
    def _set_period(self, period: str) -> None:
        today = date.today()
        start = {
            "Сегодня": today,
            "7 дней": today - timedelta(days=6),
            "30 дней": today - timedelta(days=29),
        }.get(period)
        self.period.set(period)
        for entry, value in ((self.date_from, start), (self.date_to, today if start else None)):
            entry.delete(0, "end")
            if value:
                entry.insert(0, value.strftime(DATE_FMT))

    @staticmethod
    def _parse_date(entry: ttk.Entry) -> str | None:
        raw = entry.get().strip()
        if not raw:
            return None
        for fmt in (DATE_FMT, "%Y-%m-%d"):
            try:
                return datetime.strptime(raw, fmt).date().isoformat()
            except ValueError:
                continue
        raise ValueError(f"дата «{raw}» — нужен формат ДД.ММ.ГГГГ")

    def _query_params(self) -> dict[str, str]:
        params = {"tz": self.tz, "reply_window_hours": self.window.get()}
        campaign_id = self._campaign_ids.get(self.campaign.get())
        if campaign_id:
            params["campaign_id"] = campaign_id
        platform = {v: k for k, v in PLATFORM_LABELS.items()}.get(self.platform.get())
        if platform:
            params["platform"] = platform
        date_from, date_to = self._parse_date(self.date_from), self._parse_date(self.date_to)
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        return params

    # ---------- data ----------
    def refresh(self) -> None:
        try:
            params = self._query_params()
        except ValueError as exc:
            self.app.flash_status(f"Статистика: {exc}", is_err=True)
            return
        group = GROUPINGS[self.group.get()]

        def fetch():
            campaigns = self.api(self.data_url, "/campaigns")
            total = self.api(self.data_url, "/stats/messages?" + urlencode(params))
            grouped = self.api(self.data_url, "/stats/messages?" + urlencode({**params, "group_by": group}))
            return campaigns, total["rows"][0], grouped["rows"]

        def done(result):
            campaigns, total, rows = result
            self._update_campaigns(campaigns)
            self._render_summary(total)
            self._render_table(group, rows)

        def error(exc):
            self.app.flash_status(f"Не удалось загрузить статистику: {exc}", is_err=True)

        self.app.run_bg(fetch, done, error, dedupe_key="stats")

    def _update_campaigns(self, campaigns: list[dict]) -> None:
        self._campaign_ids = {}
        for c in sorted(campaigns, key=lambda c: c.get("created_at") or "", reverse=True):
            name = c["name"]
            if name in self._campaign_ids:  # одинаковые имена различаем по началу id
                name = f"{name} ({c['id'][:6]})"
            self._campaign_ids[name] = c["id"]
        current = self.campaign.get()
        self.campaign.configure(values=[ALL, *self._campaign_ids])
        if current != ALL and current not in self._campaign_ids:
            self.campaign.set(ALL)

    def _render_summary(self, total: dict) -> None:
        self._window_title.configure(text=f"Ответили за {self.window.get()} ч")
        lbl = self.summary_labels
        lbl["sent"].configure(text=f"{total['sent']}  ({total['leads']} лидов)")
        lbl["replied"].configure(text=f"{total['replied']}  ·  {_pct(total['reply_rate'])}")
        lbl["window"].configure(text=f"{total['replied_in_window']}  ·  {_pct(total['reply_rate_in_window'])}")
        lbl["median"].configure(text=_hours(total["median_reply_hours"]))
        lbl["failed"].configure(text=f"{total['failed']} / {total['pending']}")

    def _key_values(self, group: str, row: dict) -> list[str]:
        campaign = row.get("campaign_name") or "без кампании"
        if group == "variant":
            variant = row.get("template_variant") or "?"
            label = f"Повтор ({variant})" if row.get("kind") == "followup" else f"Шаблон {variant}"
            return [campaign, label]
        if group == "campaign":
            return [campaign]
        if group == "day":
            return [date.fromisoformat(row["day"]).strftime(DATE_FMT)]
        return [PLATFORM_LABELS.get(row.get("platform"), row.get("platform") or "?")]

    def _metric_values(self, row: dict) -> list[str]:
        return [
            str(row["sent"]),
            str(row["replied"]),
            _pct(row["reply_rate"]),
            str(row["replied_in_window"]),
            _pct(row["reply_rate_in_window"]),
            _hours(row["median_reply_hours"]),
            str(row["failed"]),
            str(row["pending"]),
        ]

    def _render_table(self, group: str, rows: list[dict]) -> None:
        self._last_rows, self._last_group = rows, group
        columns = _KEY_COLUMNS[group] + self._metric_columns()
        self.tree.delete(*self.tree.get_children())
        self.tree.configure(columns=[c[0] for c in columns])
        for col_id, title, width in columns:
            self.tree.heading(col_id, text=title)
            numeric = col_id not in ("campaign", "variant", "day", "platform")
            self.tree.column(col_id, width=width, anchor="e" if numeric else "w", stretch=not numeric)
        for row in rows:
            self.tree.insert("", "end", values=self._key_values(group, row) + self._metric_values(row))
        if not rows:
            self.app.flash_status("Статистика: за выбранный период сообщений нет")

    def on_export(self) -> None:
        if not self._last_rows:
            self.app.flash_status("Статистика: нечего выгружать — сначала «Обновить»", is_err=True)
            return
        path = filedialog.asksaveasfilename(
            parent=self, defaultextension=".csv", filetypes=[("CSV", "*.csv")],
            initialfile=f"stats_{date.today().isoformat()}.csv",
        )
        if not path:
            return
        columns = _KEY_COLUMNS[self._last_group] + self._metric_columns()
        # utf-8-sig и ";" — чтобы Excel с русской локалью открыл файл двойным кликом без мастера импорта.
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f, delimiter=";")
            writer.writerow([title for _, title, _ in columns])
            for row in self._last_rows:
                writer.writerow(self._key_values(self._last_group, row) + self._metric_values(row))
        self.app.flash_status(f"Статистика выгружена: {path}")
