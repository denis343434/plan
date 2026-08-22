"""
Десктоп-панель управления VK Lead-Gen (Tkinter, только стандартная библиотека).

Не отдельный микросервис — просто клиент, который ходит по HTTP к уже
запущенному `docker compose` стеку (Data/Parser/Messaging/Orchestrator на
localhost:8001-8004). Запуск: python app.py (из этой папки), при поднятом
`docker compose up` в корне репозитория.
"""

import json
import os
import subprocess
import sys
import threading
import tkinter as tk
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

# Локальный "гейт" перед панелью — не серьёзная аутентификация (сами API Data/Parser/
# Messaging/Orchestrator по-прежнему открыты на localhost без какой-либо проверки), а просто
# экран входа, чтобы окно не открывалось сразу для кого угодно за компьютером.
USERS = {"denis": "Password", "alexiy": "Password"}

HOST = "localhost"
DATA_URL = f"http://{HOST}:8001"
PARSER_URL = f"http://{HOST}:8002"
MESSAGING_URL = f"http://{HOST}:8003"

# Скрипт живёт в parser-service (тот же, что используется и messaging-service) — реальный
# вход в VK через настоящий Playwright-браузер, отдельно от этого приложения и его
# stdlib-only зависимостей: требует `pip install playwright httpx` и `playwright install chromium`.
VK_LOGIN_SCRIPT = Path(__file__).resolve().parent.parent / "parser-service" / "scripts" / "vk_manual_login.py"
REPO_ROOT = Path(__file__).resolve().parent.parent
ORCHESTRATOR_URL = f"http://{HOST}:8004"

REFRESH_LIST_MS = 8000
REFRESH_DETAIL_MS = 3000

RU_SERVICE_NAME = {
    "data-service": "БД-сервис",
    "parser": "Парсер",
    "messaging": "Отправка",
    "orchestrator": "Оркестратор",
}
RU_PURPOSE = {"both": "оба назначения", "parsing": "парсинг", "messaging": "рассылка"}
RU_ACCOUNT_STATUS = {
    "active": "активен",
    "locked": "занят",
    "cooldown": "пауза",
    "flood": "флуд-контроль",
    "banned": "забанен",
}
RU_CAMPAIGN_STATUS = {
    "draft": "черновик",
    "running": "выполняется",
    "paused": "приостановлена",
    "completed": "завершена",
}
RU_PHASE = {
    "idle": "ожидание",
    "parsing": "парсинг",
    "messaging": "рассылка",
    "done": "готово",
    "failed": "ошибка",
    "timeout": "таймаут",
}
RU_LEAD_STATUS = {
    "new": "новый",
    "queued": "в очереди",
    "contacted": "написали",
    "replied": "ответили",
    "rejected": "отклонён",
}
RU_STATS_LABEL = {
    "new": "новые",
    "queued": "в очереди",
    "contacted": "написали",
    "replied": "ответили",
    "rejected": "отклонены",
}
RU_DELIVERY_STATUS = {"sent": "отправлено", "failed": "ошибка", "pending": "в очереди"}
RU_REPLY_STATUS = {"none": "-", "replied": "есть ответ", "no_reply": "нет ответа"}
RU_PURPOSE_REVERSE = {v: k for k, v in RU_PURPOSE.items()}
PLATFORM_LABELS = {"vk": "VK", "telegram": "Telegram", "instagram": "Instagram"}
PLATFORM_REVERSE = {v: k for k, v in PLATFORM_LABELS.items()}


class ApiError(Exception):
    pass


def api_request(base: str, path: str, method: str = "GET", body: dict | None = None, timeout: float = 5.0) -> dict | list | None:
    url = base + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        detail = exc.reason
        try:
            detail = json.loads(raw).get("detail", detail)
        except Exception:
            pass
        raise ApiError(f"{exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise ApiError(f"нет соединения: {exc.reason}") from exc


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("VK Lead-Gen — панель управления")
        self.geometry("1180x820")
        self.minsize(980, 680)

        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=24)
        style.configure("Accent.TButton", foreground="white", background="#3a6df0")
        style.map("Accent.TButton", background=[("active", "#2f5ad0")])
        style.configure("Danger.TButton", foreground="white", background="#c0392b")
        style.map("Danger.TButton", background=[("active", "#a5311f")])
        style.configure("Success.TButton", foreground="white", background="#2e9e4f")
        style.map("Success.TButton", background=[("active", "#26843f")])

        self.selected_campaign_id: str | None = None
        self.detail_after_id: str | None = None
        # Счётчики поколений для дедупликации периодических опросов (см. run_bg) — без этого
        # более медленный старый запрос может прилететь после нового и откатить таблицу назад.
        self._bg_gen: dict[str, int] = {}
        # Последний загруженный /accounts по id — используется секундным тиком (см.
        # _tick_account_cooldowns) для отсчёта кулдауна без похода в сеть на каждую секунду.
        self._accounts_cache: dict[str, dict] = {}
        # Окно "Кому написали" — держим ссылку, чтобы повторный клик по кнопке в шапке
        # поднимал уже открытое окно, а не плодил дубликаты.
        self._messages_win: tk.Toplevel | None = None
        self._messages_tree: ttk.Treeview | None = None
        self._message_reply_text: scrolledtext.ScrolledText | None = None
        self._errors_tree: ttk.Treeview | None = None
        self._replies_tree: ttk.Treeview | None = None
        self._messages_notebook: ttk.Notebook | None = None
        self._messages_error_tab: ttk.Frame | None = None
        self._messages_replies_tab: ttk.Frame | None = None
        # message_id -> ссылка на группу (сама модель Message в Data Service ссылку не хранит,
        # только lead_id — подтягиваем её через join с /leads на клиенте, см. _load_messages).
        self._message_links: dict[str, str] = {}
        # message_id -> полный текст ответа (в таблице колонка "Текст ответа" всё равно обрежется
        # по ширине ячейки — полный текст показываем в textarea под таблицей по выбору строки).
        self._message_reply_texts: dict[str, str] = {}
        # message_id новых ответов, которые уже просмотрели (кликнули по строке) — такие больше
        # не подсвечиваем синим при перезагрузке таблицы (см. _load_messages/_on_message_row_selected).
        # Живёт только в памяти клиента на время сессии — reply_status "replied" в БД не трогаем.
        self._acknowledged_reply_ids: set[str] = set()
        # логин -> account_id для комбобокса выбора аккаунта в окне "Кому написали"
        # (проверка входящих запускается за конкретный аккаунт — своя переписка/сессия).
        self._account_id_by_login: dict[str, str] = {}
        # account_id, для которого сейчас идёт фоновая проверка входящих (см. _poll_inbox_check) —
        # None, когда проверка не запущена; не даёт запустить вторую поверх идущей.
        self._inbox_check_account_id: str | None = None
        # Накопленные checked/replied по всем автопродолженным батчам одной проверки (см.
        # has_more в on_check_inbox/_poll_inbox_check) — сервер каждый батч считает с нуля
        # (limit=20 за раз), а прогресс в статусной строке должен расти по всей очереди.
        self._inbox_check_totals = {"checked": 0, "replied": 0}
        # campaign_id, для которой сейчас идёт повторная рассылка по ошибкам (см.
        # _poll_retry_send) — None, когда не запущена; не даёт запустить вторую поверх идущей.
        self._retry_send_campaign_id: str | None = None

        self._install_clipboard_shortcuts()
        self._build_layout()
        self._schedule_list_refresh()
        self.refresh_health()
        self.refresh_accounts()
        self.refresh_campaigns()
        self._tick_account_cooldowns()

    def _install_clipboard_shortcuts(self) -> None:
        # Стандартные Tk-биндинги <<Paste>>/<<Copy>>/<<Cut>> завязаны на keysym "v"/"c"/"x" —
        # на русской раскладке Windows физическая клавиша V/C/X даёт другой keysym, и Ctrl+V
        # молча ничего не делает ни в одном Entry/Text приложения. keycode — это код физической
        # клавиши, он не зависит от раскладки, поэтому дублируем действия через него.
        actions = {86: "<<Paste>>", 67: "<<Copy>>", 88: "<<Cut>>", 65: "<<SelectAll>>"}

        def on_control_key(event: tk.Event) -> str | None:
            action = actions.get(event.keycode)
            if action is None or not (event.state & 0x4):
                return None
            widget = event.widget
            if not isinstance(widget, (tk.Entry, tk.Text)):
                return None
            if action == "<<SelectAll>>":
                if isinstance(widget, tk.Text):
                    widget.tag_add("sel", "1.0", "end")
                else:
                    widget.selection_range(0, "end")
            else:
                widget.event_generate(action)
            return "break"

        self.bind_all("<Control-KeyPress>", on_control_key, add="+")

    # ---------- layout ----------
    def _build_layout(self) -> None:
        header = ttk.Frame(self, padding=(14, 10))
        header.pack(fill="x")
        ttk.Label(header, text="VK Lead-Gen — панель управления", font=("Segoe UI", 13, "bold")).pack(side="left")
        ttk.Button(header, text="Остановить приложение", style="Danger.TButton", command=self.on_stop_app).pack(side="right", padx=(12, 0))
        ttk.Button(header, text="Кому написали", command=self.open_messages_window).pack(side="right", padx=(12, 0))
        self.health_frame = ttk.Frame(header)
        self.health_frame.pack(side="right")
        self.health_dots: dict[str, ttk.Label] = {}
        for name in ("data-service", "parser", "messaging", "orchestrator"):
            lbl = ttk.Label(self.health_frame, text=f"● {RU_SERVICE_NAME[name]}", font=("Segoe UI", 9), foreground="#999")
            lbl.pack(side="left", padx=6)
            self.health_dots[name] = lbl

        self.status_bar = ttk.Label(self, text="", anchor="w", padding=(14, 3), relief="sunken")
        self.status_bar.pack(fill="x", side="bottom")

        body = ttk.Frame(self, padding=10)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=0)
        body.rowconfigure(1, weight=1)

        self._build_accounts_panel(body).grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=(0, 8))
        self._build_campaigns_panel(body).grid(row=0, column=1, sticky="nsew", padx=(6, 0), pady=(0, 8))
        self._build_detail_panel(body).grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(6, 0))

    def _build_accounts_panel(self, parent: ttk.Frame) -> ttk.LabelFrame:
        panel = ttk.LabelFrame(parent, text="Аккаунты", padding=10)

        form = ttk.Frame(panel)
        form.pack(fill="x", pady=(0, 8))
        self.acc_login = ttk.Entry(form, width=16)
        self.acc_login.grid(row=0, column=0, padx=2)
        self.acc_purpose = ttk.Combobox(form, values=list(RU_PURPOSE.values()), width=14, state="readonly")
        self.acc_purpose.set(RU_PURPOSE["both"])
        self.acc_purpose.grid(row=0, column=1, padx=2)
        self.acc_hourly = ttk.Entry(form, width=6)
        self.acc_hourly.insert(0, "100")
        self.acc_hourly.grid(row=0, column=2, padx=2)
        self.acc_daily = ttk.Entry(form, width=6)
        self.acc_daily.insert(0, "1000")
        self.acc_daily.grid(row=0, column=3, padx=2)
        ttk.Button(form, text="Создать", style="Accent.TButton", command=self.on_create_account).grid(row=0, column=4, padx=4)
        self._placeholder(self.acc_login, "логин")

        cols = ("login", "purpose", "status", "session", "usage", "cooldown")
        self.acc_tree = ttk.Treeview(panel, columns=cols, show="headings", height=6)
        for col, title, width in (
            ("login", "Логин", 130),
            ("purpose", "Назначение", 100),
            ("status", "Статус", 90),
            ("session", "Вход в VK", 90),
            ("usage", "Нагрузка", 130),
            ("cooldown", "Кулдаун", 80),
        ):
            self.acc_tree.heading(col, text=title)
            self.acc_tree.column(col, width=width, anchor="w")
        self.acc_tree.tag_configure("no_session", foreground="#c0392b")
        self.acc_tree.tag_configure("has_session", foreground="#2e9e4f")
        self.acc_tree.pack(fill="both", expand=True)

        acc_actions = ttk.Frame(panel)
        acc_actions.pack(fill="x", pady=(6, 0))
        ttk.Button(acc_actions, text="Тестовая сессия для выбранного", command=self.on_set_session).pack(side="left")
        ttk.Button(acc_actions, text="Войти в VK", command=self.on_vk_login).pack(side="left", padx=(6, 0))
        ttk.Button(acc_actions, text="Остановить аккаунт", style="Danger.TButton", command=self.on_pause_account).pack(side="left", padx=(6, 0))
        ttk.Button(acc_actions, text="Продолжить", style="Success.TButton", command=self.on_resume_account).pack(side="left", padx=(6, 0))
        ttk.Button(acc_actions, text="Удалить аккаунт", style="Danger.TButton", command=self.on_delete_account).pack(side="left", padx=(6, 0))
        return panel

    def _build_campaigns_panel(self, parent: ttk.Frame) -> ttk.LabelFrame:
        panel = ttk.LabelFrame(parent, text="Кампании", padding=10)

        form = ttk.Frame(panel)
        form.pack(fill="x", pady=(0, 8))
        self.camp_name = ttk.Entry(form, width=16)
        self.camp_name.grid(row=0, column=0, padx=2)
        self._placeholder(self.camp_name, "название")
        self.camp_platform = ttk.Combobox(form, values=list(PLATFORM_LABELS.values()), width=10, state="readonly")
        self.camp_platform.set(PLATFORM_LABELS["vk"])
        self.camp_platform.grid(row=0, column=1, padx=2)
        self.camp_keyword = ttk.Entry(form, width=14)
        self.camp_keyword.grid(row=0, column=2, padx=2)
        self._placeholder(self.camp_keyword, "ключевое слово")
        ttk.Button(form, text="Создать", style="Accent.TButton", command=self.on_create_campaign).grid(row=0, column=3, padx=4)

        cols = ("name", "keyword", "status")
        self.camp_tree = ttk.Treeview(panel, columns=cols, show="headings", height=6)
        for col, title, width in (("name", "Название", 150), ("keyword", "Ключевое слово", 140), ("status", "Статус", 110)):
            self.camp_tree.heading(col, text=title)
            self.camp_tree.column(col, width=width, anchor="w")
        self.camp_tree.pack(fill="both", expand=True)
        self.camp_tree.bind("<<TreeviewSelect>>", self.on_select_campaign)

        camp_actions = ttk.Frame(panel)
        camp_actions.pack(fill="x", pady=(6, 0))
        ttk.Button(camp_actions, text="Удалить кампанию", style="Danger.TButton", command=self.on_delete_campaign).pack(side="left")
        return panel

    def _build_detail_panel(self, parent: ttk.Frame) -> ttk.LabelFrame:
        panel = ttk.LabelFrame(parent, text="Кампания: (не выбрана)", padding=10)
        self.detail_frame = panel

        top = ttk.Frame(panel)
        top.pack(fill="x")
        self.detail_stats = ttk.Label(top, text="", font=("Segoe UI", 10))
        self.detail_stats.pack(side="left")
        btns = ttk.Frame(top)
        btns.pack(side="right")
        ttk.Label(btns, text="групп парсить:").pack(side="left", padx=(0, 3))
        self.max_groups_entry = ttk.Entry(btns, width=5)
        self.max_groups_entry.insert(0, "20")
        self.max_groups_entry.pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="Запустить", style="Accent.TButton", command=self.on_start_campaign).pack(side="left", padx=2)
        ttk.Button(btns, text="Повторить с ошибками", command=self.on_retry_failed_send).pack(side="left", padx=2)
        ttk.Button(btns, text="Обновить", command=self.refresh_detail).pack(side="left", padx=2)
        ttk.Button(btns, text="Удалить кампанию", style="Danger.TButton", command=self.on_delete_campaign).pack(side="left", padx=2)

        self.detail_status = ttk.Label(panel, text="", foreground="#555")
        self.detail_status.pack(fill="x", pady=(6, 2))

        progress_row = ttk.Frame(panel)
        progress_row.pack(fill="x", pady=(0, 10))
        self.detail_progress_label = ttk.Label(progress_row, text="", foreground="#555")
        self.detail_progress_label.pack(side="left")
        self.detail_progress_bar = ttk.Progressbar(progress_row, mode="determinate", length=220)
        # По умолчанию скрыт (pack не вызван) — появляется только когда есть что показывать,
        # чтобы не занимать место, пока кампания не запущена/не в процессе парсинга.

        ttk.Label(panel, text="ШАБЛОН СООБЩЕНИЯ", font=("Segoe UI", 9, "bold"), foreground="#777").pack(anchor="w")
        tpl_form = ttk.Frame(panel)
        tpl_form.pack(fill="x", pady=(2, 4))
        self.tpl_variant = ttk.Combobox(tpl_form, values=["A", "B"], width=4, state="readonly")
        self.tpl_variant.set("A")
        self.tpl_variant.pack(side="left", padx=(0, 6))
        self.tpl_body = ttk.Entry(tpl_form)
        self.tpl_body.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self._placeholder(self.tpl_body, "Здравствуйте, {{org_name}}!")
        ttk.Button(tpl_form, text="Сохранить шаблон", command=self.on_save_template).pack(side="left")
        self.tpl_list_label = ttk.Label(panel, text="", foreground="#777", wraplength=1100, justify="left")
        self.tpl_list_label.pack(fill="x", pady=(0, 10))

        ttk.Label(panel, text="ЛИДЫ КАМПАНИИ", font=("Segoe UI", 9, "bold"), foreground="#777").pack(anchor="w")
        cols = ("title", "group_url", "status")
        self.leads_tree = ttk.Treeview(panel, columns=cols, show="headings", height=8)
        for col, title, width in (("title", "Название", 220), ("group_url", "Ссылка на группу", 320), ("status", "Статус", 110)):
            self.leads_tree.heading(col, text=title)
            self.leads_tree.column(col, width=width, anchor="w")
        self.leads_tree.pack(fill="both", expand=True, pady=(4, 0))
        self.leads_tree.bind("<Double-1>", self._on_lead_row_double_click)
        return panel

    @staticmethod
    def _placeholder(entry: ttk.Entry, text: str) -> None:
        entry.insert(0, text)
        entry.configure(foreground="#999")

        def on_focus_in(_event: object) -> None:
            if entry.get() == text:
                entry.delete(0, "end")
                entry.configure(foreground="black")

        def on_focus_out(_event: object) -> None:
            if not entry.get():
                entry.insert(0, text)
                entry.configure(foreground="#999")

        entry.bind("<FocusIn>", on_focus_in)
        entry.bind("<FocusOut>", on_focus_out)
        entry._placeholder_text = text  # type: ignore[attr-defined]

    @staticmethod
    def _value(entry: ttk.Entry) -> str:
        text = entry.get()
        return "" if text == getattr(entry, "_placeholder_text", None) else text

    # ---------- async helper ----------
    def run_bg(self, fn, on_done=None, on_error=None, dedupe_key: str | None = None) -> None:
        # dedupe_key нужен для периодических опросов одного и того же эндпоинта: запросы
        # уходят в отдельных потоках и могут завершиться в любом порядке, поэтому без проверки
        # "я всё ещё последний выпущенный запрос с этим ключом" более старый, но медленный ответ
        # мог прилететь позже нового и молча откатить UI на устаревшие данные.
        gen = None
        if dedupe_key is not None:
            gen = self._bg_gen.get(dedupe_key, 0) + 1
            self._bg_gen[dedupe_key] = gen

        def is_current() -> bool:
            return dedupe_key is None or self._bg_gen.get(dedupe_key) == gen

        def worker() -> None:
            try:
                result = fn()
            except Exception as exc:  # noqa: BLE001 - показываем любую ошибку пользователю
                if on_error and is_current():
                    self.after(0, lambda: on_error(exc))
                return
            if on_done and is_current():
                self.after(0, lambda: on_done(result))

        threading.Thread(target=worker, daemon=True).start()

    def flash_status(self, msg: str, is_err: bool = False) -> None:
        self.status_bar.configure(text=msg, foreground="#c0392b" if is_err else "#2c3e50")

    # ---------- health ----------
    def refresh_health(self) -> None:
        services = [("data-service", DATA_URL), ("parser", PARSER_URL), ("messaging", MESSAGING_URL), ("orchestrator", ORCHESTRATOR_URL)]

        def check_all():
            results = []
            for name, base in services:
                try:
                    api_request(base, "/health")
                    results.append((name, True))
                except ApiError:
                    results.append((name, False))
            return results

        def done(results):
            for name, ok in results:
                self.health_dots[name].configure(foreground="#2e9e4f" if ok else "#c0392b")

        self.run_bg(check_all, on_done=done, dedupe_key="health")

    # ---------- accounts ----------
    @staticmethod
    def _cooldown_text(account: dict) -> str:
        # cooldown_until (или locked_until — во время самого цикла парсинга/рассылки) отдаётся
        # Data Service как naive UTC ISO-строка (см. Account.cooldown_until в data-service) —
        # datetime.now(timezone.utc) на клиенте и сравниваем с ним напрямую как UTC.
        until_raw = account.get("cooldown_until") or account.get("locked_until")
        if not until_raw:
            return "-"
        try:
            until_dt = datetime.fromisoformat(until_raw.replace("Z", "+00:00"))
        except ValueError:
            return "-"
        if until_dt.tzinfo is None:
            until_dt = until_dt.replace(tzinfo=timezone.utc)
        remaining = int((until_dt - datetime.now(timezone.utc)).total_seconds())
        if remaining <= 0:
            return "-"
        minutes, seconds = divmod(remaining, 60)
        return f"{minutes}:{seconds:02d}"

    def refresh_accounts(self) -> None:
        def fetch():
            return api_request(DATA_URL, "/accounts")

        def done(accounts):
            self._accounts_cache = {a["id"]: a for a in accounts}
            self.acc_tree.delete(*self.acc_tree.get_children())
            for a in accounts:
                usage = f"{a['hourly_used']}/{a['hourly_limit']} ч · {a['daily_used']}/{a['daily_limit']} сут"
                has_session = a.get("has_session", False)
                session_text = "есть" if has_session else "нет входа"
                self.acc_tree.insert(
                    "", "end", iid=a["id"],
                    values=(
                        a["login"], RU_PURPOSE.get(a["purpose"], a["purpose"]),
                        RU_ACCOUNT_STATUS.get(a["status"], a["status"]), session_text, usage,
                        self._cooldown_text(a),
                    ),
                    tags=("has_session" if has_session else "no_session",),
                )

        def error(exc):
            self.flash_status(f"Не удалось загрузить аккаунты: {exc}", is_err=True)

        self.run_bg(fetch, on_done=done, on_error=error, dedupe_key="accounts")

    def _tick_account_cooldowns(self) -> None:
        # Отдельный секундный тик поверх обычного refresh_accounts (раз в REFRESH_LIST_MS) —
        # без него счётчик дёргался бы раз в 8 секунд рывками вместо плавного обратного отсчёта.
        # Считает по локально закэшированным данным, без похода в сеть.
        for account_id, account in self._accounts_cache.items():
            if self.acc_tree.exists(account_id):
                self.acc_tree.set(account_id, "cooldown", self._cooldown_text(account))
        self.after(1000, self._tick_account_cooldowns)

    def on_create_account(self) -> None:
        payload = {
            "platform": "vk",
            "login": self._value(self.acc_login),
            "purpose": RU_PURPOSE_REVERSE.get(self.acc_purpose.get(), "both"),
            "hourly_limit": int(self._value(self.acc_hourly) or 100),
            "daily_limit": int(self._value(self.acc_daily) or 1000),
        }
        if not payload["login"]:
            messagebox.showwarning("Аккаунты", "Введите логин")
            return

        def create():
            return api_request(DATA_URL, "/accounts", method="POST", body=payload)

        def done(_result):
            self.flash_status("Аккаунт создан")
            self.refresh_accounts()

        def error(exc):
            messagebox.showerror("Ошибка", str(exc))

        self.run_bg(create, on_done=done, on_error=error)

    def on_set_session(self) -> None:
        selected = self.acc_tree.selection()
        if not selected:
            messagebox.showinfo("Аккаунты", "Сначала выберите аккаунт в таблице")
            return
        account_id = selected[0]

        def set_session():
            return api_request(DATA_URL, f"/accounts/{account_id}/session", method="PUT", body={"storage_state": {"cookies": [], "origins": []}})

        def done(_result):
            self.flash_status("Тестовая сессия установлена")

        def error(exc):
            messagebox.showerror("Ошибка", str(exc))

        self.run_bg(set_session, on_done=done, on_error=error)

    def on_pause_account(self) -> None:
        selected = self.acc_tree.selection()
        if not selected:
            messagebox.showinfo("Аккаунты", "Сначала выберите аккаунт в таблице")
            return
        account_id = selected[0]
        login = self.acc_tree.item(account_id, "values")[0]
        if not messagebox.askyesno(
            "Остановить аккаунт",
            f"Остановить аккаунт «{login}» на 7 дней (аварийная пауза)?\n\n"
            "Он перестанет выдаваться под парсинг/рассылку, пока пауза не истечёт "
            "или его не разблокируют вручную в БД.",
        ):
            return

        def pause():
            return api_request(ORCHESTRATOR_URL, f"/accounts/{account_id}/pause", method="POST", body={})

        def done(_result):
            self.flash_status(f"Аккаунт «{login}» остановлен")
            self.refresh_accounts()

        def error(exc):
            messagebox.showerror("Ошибка", str(exc))

        self.run_bg(pause, on_done=done, on_error=error)

    def on_resume_account(self) -> None:
        selected = self.acc_tree.selection()
        if not selected:
            messagebox.showinfo("Аккаунты", "Сначала выберите аккаунт в таблице")
            return
        account_id = selected[0]
        login = self.acc_tree.item(account_id, "values")[0]

        def resume():
            return api_request(DATA_URL, f"/accounts/{account_id}/activate", method="POST")

        def done(_result):
            self.flash_status(f"Аккаунт «{login}» снова активен")
            self.refresh_accounts()

        def error(exc):
            messagebox.showerror("Ошибка", str(exc))

        self.run_bg(resume, on_done=done, on_error=error)

    def on_delete_account(self) -> None:
        selected = self.acc_tree.selection()
        if not selected:
            messagebox.showinfo("Аккаунты", "Сначала выберите аккаунт в таблице")
            return
        account_id = selected[0]
        login = self.acc_tree.item(account_id, "values")[0]
        if not messagebox.askyesno(
            "Удалить аккаунт",
            f"Удалить аккаунт «{login}» безвозвратно?\n\n"
            "Вместе с ним удалится его сохранённая сессия и вся история отправленных "
            "им сообщений.",
        ):
            return

        def delete():
            return api_request(DATA_URL, f"/accounts/{account_id}", method="DELETE")

        def done(_result):
            self.flash_status(f"Аккаунт «{login}» удалён")
            self.refresh_accounts()

        def error(exc):
            messagebox.showerror("Ошибка", str(exc))

        self.run_bg(delete, on_done=done, on_error=error)

    def on_vk_login(self) -> None:
        selected = self.acc_tree.selection()
        if not selected:
            messagebox.showinfo("Вход в VK", "Сначала выберите аккаунт в таблице")
            return
        account_id = selected[0]
        if not VK_LOGIN_SCRIPT.exists():
            messagebox.showerror("Вход в VK", f"Не найден скрипт: {VK_LOGIN_SCRIPT}")
            return

        if not messagebox.askyesno(
            "Вход в VK",
            "Откроется отдельное окно браузера — войди в VK вручную (с 2FA, если есть), "
            "затем вернись в открывшуюся консоль и нажми Enter, чтобы сохранить сессию.\n\n"
            "Продолжить?",
        ):
            return

        try:
            subprocess.Popen(
                [sys.executable, str(VK_LOGIN_SCRIPT), "--account-id", account_id, "--data-service-url", DATA_URL],
                creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0,
                cwd=str(VK_LOGIN_SCRIPT.parent.parent),
            )
            self.flash_status("Окно логина VK открывается в отдельной консоли…")
        except OSError as exc:
            messagebox.showerror("Вход в VK", f"Не удалось запустить скрипт логина: {exc}")

    # ---------- campaigns ----------
    def refresh_campaigns(self) -> None:
        def fetch():
            return api_request(DATA_URL, "/campaigns")

        def done(campaigns):
            self.camp_tree.delete(*self.camp_tree.get_children())
            for c in campaigns:
                self.camp_tree.insert(
                    "", "end", iid=c["id"],
                    values=(c["name"], c["keyword"], RU_CAMPAIGN_STATUS.get(c["status"], c["status"])),
                )
            if self.selected_campaign_id and self.camp_tree.exists(self.selected_campaign_id):
                self.camp_tree.selection_set(self.selected_campaign_id)

        def error(exc):
            self.flash_status(f"Не удалось загрузить кампании: {exc}", is_err=True)

        self.run_bg(fetch, on_done=done, on_error=error, dedupe_key="campaigns")

    def on_create_campaign(self) -> None:
        payload = {
            "name": self._value(self.camp_name),
            "platform": PLATFORM_REVERSE.get(self.camp_platform.get(), "vk"),
            "keyword": self._value(self.camp_keyword),
        }
        if not payload["name"] or not payload["keyword"]:
            messagebox.showwarning("Кампании", "Заполните название и ключевое слово")
            return

        def create():
            return api_request(ORCHESTRATOR_URL, "/campaigns", method="POST", body=payload)

        def done(campaign):
            self.flash_status("Кампания создана")
            self.refresh_campaigns()
            self.select_campaign(campaign["id"], campaign["name"])

        def error(exc):
            messagebox.showerror("Ошибка", str(exc))

        self.run_bg(create, on_done=done, on_error=error)

    def on_delete_campaign(self) -> None:
        selected = self.camp_tree.selection()
        if not selected:
            messagebox.showinfo("Кампании", "Сначала выберите кампанию в таблице")
            return
        campaign_id = selected[0]
        name = self.camp_tree.item(campaign_id, "values")[0]
        if not messagebox.askyesno(
            "Удалить кампанию",
            f"Удалить кампанию «{name}» безвозвратно?\n\n"
            "Найденные лиды и шаблоны сохранятся в базе, но потеряют привязку к этой кампании.",
        ):
            return

        def delete():
            return api_request(DATA_URL, f"/campaigns/{campaign_id}", method="DELETE")

        def done(_result):
            self.flash_status(f"Кампания «{name}» удалена")
            if self.selected_campaign_id == campaign_id:
                self.selected_campaign_id = None
                if self.detail_after_id:
                    self.after_cancel(self.detail_after_id)
                    self.detail_after_id = None
                self.detail_frame.configure(text="Кампания: (не выбрана)")
                self.detail_stats.configure(text="")
                self.detail_status.configure(text="")
                self.leads_tree.delete(*self.leads_tree.get_children())
                self.tpl_list_label.configure(text="")
            self.refresh_campaigns()

        def error(exc):
            messagebox.showerror("Ошибка", str(exc))

        self.run_bg(delete, on_done=done, on_error=error)

    def on_select_campaign(self, _event: object) -> None:
        selected = self.camp_tree.selection()
        if not selected:
            return
        campaign_id = selected[0]
        name = self.camp_tree.item(campaign_id, "values")[0]
        self.select_campaign(campaign_id, name)

    def select_campaign(self, campaign_id: str, name: str) -> None:
        self.selected_campaign_id = campaign_id
        self.detail_frame.configure(text=f"Кампания: {name}  ({campaign_id})")
        if self.detail_after_id:
            self.after_cancel(self.detail_after_id)
        self.refresh_detail()
        self._schedule_detail_refresh()

    def _schedule_detail_refresh(self) -> None:
        self.detail_after_id = self.after(REFRESH_DETAIL_MS, self._detail_tick)

    def _detail_tick(self) -> None:
        self.refresh_detail()
        self._schedule_detail_refresh()

    def _schedule_list_refresh(self) -> None:
        def tick():
            self.refresh_health()
            self.refresh_accounts()
            self.refresh_campaigns()
            self.after(REFRESH_LIST_MS, tick)

        self.after(REFRESH_LIST_MS, tick)

    def on_start_campaign(self) -> None:
        if not self.selected_campaign_id:
            return
        campaign_id = self.selected_campaign_id

        raw_limit = self.max_groups_entry.get().strip()
        path = f"/campaigns/{campaign_id}/start"
        if raw_limit:
            try:
                max_groups = int(raw_limit)
            except ValueError:
                messagebox.showwarning("Кампании", "«Групп парсить» должно быть числом (или оставь пустым — без ограничения)")
                return
            if max_groups <= 0:
                messagebox.showwarning("Кампании", "«Групп парсить» должно быть больше нуля")
                return
            path += f"?max_groups={max_groups}"

        def start():
            return api_request(ORCHESTRATOR_URL, path, method="POST")

        def done(_result):
            self.flash_status("Кампания запущена")
            self.refresh_detail()

        def error(exc):
            messagebox.showerror("Ошибка запуска", str(exc))

        self.run_bg(start, on_done=done, on_error=error)

    def on_retry_failed_send(self) -> None:
        if not self.selected_campaign_id:
            return
        campaign_id = self.selected_campaign_id
        if self._retry_send_campaign_id is not None:
            messagebox.showinfo("Рассылка", "Повторная рассылка уже идёт, дождитесь её окончания")
            return

        self._retry_send_campaign_id = campaign_id
        self.detail_progress_bar.pack_forget()
        self.detail_progress_label.configure(text="запускаю повторную рассылку…")

        def start():
            # Напрямую в Messaging Service, в обход Orchestrator — "Запустить" гоняет весь цикл
            # заново (включая повторный парсинг), а нам нужно только повторить отправку.
            # POST /campaigns/{id}/send рассылает всем лидам со status=new — туда как раз
            # попадают лиды, у которых прошлая отправка провалилась без признаков флуда (см.
            # messaging-service/app/tasks.py:_process_lead — такие лиды НЕ переводятся в
            # "contacted", остаются "new" именно для повторной отправки), плюс любые ещё не
            # тронутые лиды.
            return api_request(MESSAGING_URL, f"/campaigns/{campaign_id}/send", method="POST")

        def started(_result):
            self.after(500, lambda: self._poll_retry_send(campaign_id))

        def error(exc):
            self._retry_send_campaign_id = None
            self.detail_progress_label.configure(text="")
            messagebox.showerror("Рассылка", str(exc))

        self.run_bg(start, on_done=started, on_error=error)

    def _poll_retry_send(self, campaign_id: str) -> None:
        if campaign_id != self.selected_campaign_id:
            self._retry_send_campaign_id = None
            return

        def fetch():
            return api_request(MESSAGING_URL, f"/campaigns/{campaign_id}/send-status", timeout=15.0)

        def done(result):
            if campaign_id != self.selected_campaign_id:
                self._retry_send_campaign_id = None
                return
            status = result["status"]
            if status in ("queued", "running"):
                self.detail_progress_label.configure(
                    text=f"Повторная рассылка: отправлено {result['sent']}, ошибок {result['failed']}, пропущено {result['skipped']}"
                )
                self.after(2000, lambda: self._poll_retry_send(campaign_id))
                return

            self._retry_send_campaign_id = None
            if status == "failed":
                self.detail_progress_label.configure(text="")
                messagebox.showerror("Рассылка", result.get("error") or "не удалось повторить рассылку")
                return
            if status == "waiting_for_account":
                self.detail_progress_label.configure(
                    text=f"Повторная рассылка приостановлена: нет свободного аккаунта "
                    f"(отправлено {result['sent']}, ошибок {result['failed']})"
                )
                self.flash_status("Повторная рассылка приостановлена: нет свободного аккаунта")
                self.refresh_leads()
                return

            self.detail_progress_label.configure(
                text=f"Повторная рассылка завершена: отправлено {result['sent']}, ошибок {result['failed']}, пропущено {result['skipped']}"
            )
            self.flash_status(f"Повторная рассылка: отправлено {result['sent']}, ошибок {result['failed']}")
            self.refresh_leads()

        def error(exc):
            self._retry_send_campaign_id = None
            self.detail_progress_label.configure(text="")
            messagebox.showerror("Рассылка", str(exc))

        self.run_bg(fetch, on_done=done, on_error=error, dedupe_key="retry_send_status")

    def refresh_detail(self) -> None:
        if not self.selected_campaign_id:
            return
        campaign_id = self.selected_campaign_id

        def fetch():
            return api_request(ORCHESTRATOR_URL, f"/campaigns/{campaign_id}")

        def done(data):
            if campaign_id != self.selected_campaign_id:
                return
            stats = data.get("stats") or {}
            self.detail_stats.configure(text="   ".join(f"{RU_STATS_LABEL.get(k, k)}: {v}" for k, v in stats.items()))
            status_text = f"фаза: {RU_PHASE.get(data['phase'], data['phase'])} · статус кампании: {RU_CAMPAIGN_STATUS.get(data['campaign']['status'], data['campaign']['status'])}"
            if data.get("error"):
                status_text += f" · ошибка: {data['error']}"
            elif data.get("note"):
                status_text += f" · {data['note']}"
            self.detail_status.configure(text=status_text, foreground="#c0392b" if data.get("error") else "#555")
            if self._retry_send_campaign_id != campaign_id:
                # Пока идёт своя повторная рассылка (см. _poll_retry_send) — Orchestrator о ней
                # не знает (её запускают напрямую в Messaging Service в обход него) и покажет
                # тут устаревший/пустой прогресс, затирая наш. Не даём ему это делать.
                self._update_progress(data.get("phase"), data.get("progress"))

        def error(exc):
            self.detail_status.configure(text=f"Ошибка загрузки статуса: {exc}", foreground="#c0392b")

        self.run_bg(fetch, on_done=done, on_error=error, dedupe_key="detail_status")
        self.refresh_leads()
        self.refresh_templates()

    def _update_progress(self, phase: str | None, progress: dict | None) -> None:
        if not progress or phase not in ("parsing", "messaging"):
            self.detail_progress_label.configure(text="")
            self.detail_progress_bar.pack_forget()
            return

        if phase == "parsing":
            checked = progress.get("checked", 0)
            total = progress.get("total", 0)
            found = progress.get("found", 0)
            if total > 0:
                self.detail_progress_bar["maximum"] = total
                self.detail_progress_bar["value"] = checked
                self.detail_progress_bar.pack(side="left", padx=(10, 0))
                self.detail_progress_label.configure(
                    text=f"Парсинг: проверено {checked}/{total} групп, подходит (без сайта): {found}"
                )
            else:
                self.detail_progress_bar.pack_forget()
                self.detail_progress_label.configure(text="Парсинг: идёт поиск групп…")
        else:  # messaging
            self.detail_progress_bar.pack_forget()
            sent = progress.get("sent", 0)
            failed = progress.get("failed", 0)
            skipped = progress.get("skipped", 0)
            self.detail_progress_label.configure(text=f"Рассылка: отправлено {sent}, ошибок {failed}, пропущено {skipped}")

    def refresh_leads(self) -> None:
        campaign_id = self.selected_campaign_id
        if not campaign_id:
            return

        def fetch():
            return api_request(DATA_URL, f"/leads?campaign_id={campaign_id}")

        def done(leads):
            if campaign_id != self.selected_campaign_id:
                return
            self.leads_tree.delete(*self.leads_tree.get_children())
            for lead in leads:
                self.leads_tree.insert(
                    "", "end", iid=lead["id"],
                    values=(lead.get("title") or "", lead.get("group_url") or "", RU_LEAD_STATUS.get(lead["status"], lead["status"])),
                )

        self.run_bg(fetch, on_done=done, dedupe_key="leads")

    def _on_lead_row_double_click(self, _event: object) -> None:
        selected = self.leads_tree.selection()
        if not selected:
            return
        group_url = self.leads_tree.item(selected[0], "values")[1]
        if group_url:
            webbrowser.open(group_url)

    def refresh_templates(self) -> None:
        campaign_id = self.selected_campaign_id
        if not campaign_id:
            return

        def fetch():
            return api_request(DATA_URL, "/templates")

        def done(templates):
            if campaign_id != self.selected_campaign_id:
                return
            own = [t for t in templates if t.get("campaign_id") == campaign_id]
            text = "сохранено: " + " · ".join(f'{t["variant"]} — "{t["body"]}"' for t in own) if own else "шаблонов пока нет"
            self.tpl_list_label.configure(text=text)

        self.run_bg(fetch, on_done=done, dedupe_key="templates")

    def on_save_template(self) -> None:
        if not self.selected_campaign_id:
            messagebox.showinfo("Шаблон", "Сначала выберите кампанию")
            return
        campaign_id = self.selected_campaign_id
        payload = {"campaign_id": campaign_id, "variant": self.tpl_variant.get(), "body": self._value(self.tpl_body)}
        if not payload["body"]:
            messagebox.showwarning("Шаблон", "Введите текст сообщения")
            return

        def save():
            return api_request(DATA_URL, "/templates", method="POST", body=payload)

        def done(_result):
            self.flash_status("Шаблон сохранён")
            self.refresh_templates()

        def error(exc):
            messagebox.showerror("Ошибка", str(exc))

        self.run_bg(save, on_done=done, on_error=error)

    # ---------- messages ("Кому написали") ----------
    def open_messages_window(self) -> None:
        if self._messages_win is not None and self._messages_win.winfo_exists():
            self._messages_win.deiconify()
            self._messages_win.lift()
            self._refresh_message_account_choices()
            self._load_messages()
            return

        win = tk.Toplevel(self)
        win.title("Кому написали")
        win.geometry("1180x640")
        win.minsize(900, 420)
        self._messages_win = win

        top = ttk.Frame(win, padding=(10, 10, 10, 4))
        top.pack(fill="x")
        ttk.Button(top, text="Обновить", command=self._load_messages).pack(side="left")
        ttk.Button(top, text="Открыть ссылку", style="Accent.TButton", command=self._open_selected_message_link).pack(side="left", padx=(6, 0))
        ttk.Label(top, text="или двойной клик по строке", foreground="#777").pack(side="left", padx=(8, 0))

        inbox_bar = ttk.Frame(win, padding=(10, 0, 10, 8))
        inbox_bar.pack(fill="x")
        ttk.Label(inbox_bar, text="Проверить входящие для аккаунта:").pack(side="left")
        self.msg_account_combo = ttk.Combobox(inbox_bar, width=18, state="readonly")
        self.msg_account_combo.pack(side="left", padx=(6, 6))
        ttk.Button(inbox_bar, text="Проверить ответы", style="Accent.TButton", command=self.on_check_inbox).pack(side="left")
        self.inbox_check_status = ttk.Label(inbox_bar, text="", foreground="#777")
        self.inbox_check_status.pack(side="left", padx=(10, 0))

        notebook = ttk.Notebook(win)
        notebook.pack(fill="both", expand=True, padx=10, pady=(4, 4))
        all_tab = ttk.Frame(notebook)
        error_tab = ttk.Frame(notebook)
        replies_tab = ttk.Frame(notebook)
        notebook.add(all_tab, text="Отправленные")
        notebook.add(error_tab, text="Ошибки")
        notebook.add(replies_tab, text="Ответили")
        self._messages_notebook = notebook
        self._messages_error_tab = error_tab
        self._messages_replies_tab = replies_tab

        self._messages_tree = self._build_messages_tree(all_tab)
        self._errors_tree = self._build_messages_tree(error_tab)
        self._replies_tree = self._build_messages_tree(replies_tab)

        reply_frame = ttk.Frame(win, padding=(10, 0, 10, 10))
        reply_frame.pack(fill="x")
        ttk.Label(reply_frame, text="Текст ответа:").pack(anchor="w")
        reply_text = scrolledtext.ScrolledText(reply_frame, height=5, wrap="word")
        reply_text.configure(state="disabled")
        reply_text.pack(fill="x", pady=(2, 0))
        self._message_reply_text = reply_text

        self._refresh_message_account_choices()
        self._load_messages()

    def _build_messages_tree(self, parent: ttk.Frame) -> ttk.Treeview:
        cols = ("date", "campaign", "title", "group_url", "status", "reply", "reply_text", "error")
        tree = ttk.Treeview(parent, columns=cols, show="headings")
        for col, title, width in (
            ("date", "Когда", 120),
            ("campaign", "Кампания", 100),
            ("title", "Группа", 170),
            ("group_url", "Ссылка", 170),
            ("status", "Статус", 90),
            ("reply", "Ответ", 90),
            ("reply_text", "Текст ответа", 220),
            ("error", "Причина ошибки", 200),
        ):
            tree.heading(col, text=title)
            tree.column(col, width=width, anchor="w")
        tree.tag_configure("sent", foreground="#2e9e4f")
        tree.tag_configure("failed", foreground="#c0392b")
        tree.tag_configure("replied", background="#3a6df0", foreground="white")
        tree.pack(fill="both", expand=True)
        tree.bind("<Double-1>", lambda _event, t=tree: self._open_selected_message_link(t))
        tree.bind("<<TreeviewSelect>>", lambda _event, t=tree: self._on_message_row_selected(t))
        return tree

    def _active_messages_tree(self) -> ttk.Treeview | None:
        # "Открыть ссылку" в шапке окна должна работать с той таблицей, что сейчас видна —
        # любая из трёх вкладок, а не всегда с первой (см. _build_messages_tree, который создаёт
        # все таблицы одинаковыми и биндит на них общие обработчики).
        notebook = self._messages_notebook
        if notebook is None:
            return self._messages_tree
        current = notebook.select()
        if self._messages_error_tab is not None and current == str(self._messages_error_tab):
            return self._errors_tree
        if self._messages_replies_tab is not None and current == str(self._messages_replies_tab):
            return self._replies_tree
        return self._messages_tree

    def _load_messages(self) -> None:
        tree = self._messages_tree
        errors_tree = self._errors_tree
        replies_tree = self._replies_tree
        if tree is None or errors_tree is None or replies_tree is None:
            return

        def fetch():
            # limit=1000 — /leads по умолчанию отдаёт только 50, а сообщения нужно сопоставлять
            # с лидами всех кампаний сразу (join делаем на клиенте — Data Service его не отдаёт).
            messages = api_request(DATA_URL, "/messages") or []
            leads = api_request(DATA_URL, "/leads?limit=1000") or []
            campaigns = api_request(DATA_URL, "/campaigns") or []
            return messages, leads, campaigns

        def done(result):
            messages, leads, campaigns = result
            if self._messages_tree is not tree or not tree.winfo_exists():
                return
            leads_by_id = {lead["id"]: lead for lead in leads}
            campaign_names = {c["id"]: c["name"] for c in campaigns}

            tree.delete(*tree.get_children())
            errors_tree.delete(*errors_tree.get_children())
            replies_tree.delete(*replies_tree.get_children())
            self._message_links = {}
            self._message_reply_texts = {}
            error_count = 0
            reply_count = 0
            for m in sorted(messages, key=lambda m: m["sent_at"], reverse=True):
                lead = leads_by_id.get(m["lead_id"], {})
                title = lead.get("title") or "-"
                group_url = lead.get("group_url") or ""
                campaign_name = campaign_names.get(lead.get("campaign_id"), "-")
                status = m["delivery_status"]
                status_text = RU_DELIVERY_STATUS.get(status, status)
                reply_status = m.get("reply_status", "none")
                reply_text = RU_REPLY_STATUS.get(reply_status, reply_status)
                when = (m.get("sent_at") or "")[:16].replace("T", " ")
                # replied — самое заметное (это и есть входящее сообщение), пока строку не
                # просмотрели (см. _on_message_row_selected); иначе как раньше: failed/sent по
                # статусу доставки.
                if reply_status == "replied" and m["id"] not in self._acknowledged_reply_ids:
                    tag = "replied"
                else:
                    tag = "sent" if status == "sent" else "failed"
                row_values = (
                    when, campaign_name, title, group_url, status_text,
                    reply_text, m.get("reply_preview") or "", m.get("error_reason") or "",
                )
                if status == "failed":
                    errors_tree.insert("", "end", iid=m["id"], values=row_values, tags=(tag,))
                    error_count += 1
                elif reply_status == "replied":
                    replies_tree.insert("", "end", iid=m["id"], values=row_values, tags=(tag,))
                    reply_count += 1
                else:
                    tree.insert("", "end", iid=m["id"], values=row_values, tags=(tag,))
                self._message_links[m["id"]] = group_url
                self._message_reply_texts[m["id"]] = m.get("reply_preview") or ""

            if self._messages_notebook is not None and self._messages_error_tab is not None:
                self._messages_notebook.tab(self._messages_error_tab, text=f"Ошибки ({error_count})")
            if self._messages_notebook is not None and self._messages_replies_tab is not None:
                self._messages_notebook.tab(self._messages_replies_tab, text=f"Ответили ({reply_count})")
            self._on_message_row_selected(self._active_messages_tree())

        def error(exc):
            self.flash_status(f"Не удалось загрузить сообщения: {exc}", is_err=True)

        self.run_bg(fetch, on_done=done, on_error=error, dedupe_key="messages")

    def _on_message_row_selected(self, tree: ttk.Treeview | None = None) -> None:
        tree = tree if tree is not None else self._active_messages_tree()
        reply_text = self._message_reply_text
        if tree is None or reply_text is None or not reply_text.winfo_exists():
            return
        selected = tree.selection()
        text = self._message_reply_texts.get(selected[0], "") if selected else ""
        if selected:
            message_id = selected[0]
            if "replied" in tree.item(message_id, "tags"):
                self._acknowledged_reply_ids.add(message_id)
                tree.item(message_id, tags=("sent",))
        reply_text.configure(state="normal")
        reply_text.delete("1.0", "end")
        reply_text.insert("1.0", text)
        reply_text.configure(state="disabled")

    def _open_selected_message_link(self, tree: ttk.Treeview | None = None) -> None:
        tree = tree if tree is not None else self._active_messages_tree()
        if tree is None:
            return
        selected = tree.selection()
        if not selected:
            messagebox.showinfo("Кому написали", "Сначала выберите строку в таблице")
            return
        url = self._message_links.get(selected[0])
        if not url:
            messagebox.showinfo("Кому написали", "У этой записи нет ссылки на группу")
            return
        webbrowser.open(url)

    def _refresh_message_account_choices(self) -> None:
        # Только messaging/both — у "чисто парсинговых" аккаунтов отправленных сообщений
        # не бывает, проверять для них входящие нечего.
        accounts = [a for a in self._accounts_cache.values() if a.get("purpose") in ("messaging", "both")]
        self._account_id_by_login = {a["login"]: a["id"] for a in accounts}
        logins = list(self._account_id_by_login)
        self.msg_account_combo.configure(values=logins)
        if logins and self.msg_account_combo.get() not in logins:
            self.msg_account_combo.set(logins[0])

    def on_check_inbox(self) -> None:
        login = self.msg_account_combo.get()
        account_id = self._account_id_by_login.get(login)
        if not account_id:
            messagebox.showinfo("Входящие", "Выберите аккаунт для проверки — сначала войдите в VK хотя бы под одним")
            return
        if self._inbox_check_account_id is not None:
            messagebox.showinfo("Входящие", "Проверка уже идёт, дождитесь её окончания")
            return

        self._inbox_check_account_id = account_id
        self._inbox_check_totals = {"checked": 0, "replied": 0}
        self.inbox_check_status.configure(text="запускаю проверку…")
        self._start_inbox_check_batch(account_id)

    def _start_inbox_check_batch(self, account_id: str) -> None:
        def start():
            # Messaging Service запускает проверку фоновой задачей и сразу отвечает (не ждёт
            # обхода всех переписок в реальном браузере) — прогресс опрашивается отдельно,
            # см. _poll_inbox_check. Раньше это был один блокирующий запрос на весь батч —
            # при реальном VK-аккаунте с десятками сообщений выглядело как зависшее окно.
            return api_request(MESSAGING_URL, f"/inbox/check?account_id={account_id}", method="POST")

        def started(_result):
            self.after(500, lambda: self._poll_inbox_check(account_id))

        def error(exc):
            self._inbox_check_account_id = None
            self.inbox_check_status.configure(text="")
            messagebox.showerror("Входящие", str(exc))

        self.run_bg(start, on_done=started, on_error=error)

    def _poll_inbox_check(self, account_id: str) -> None:
        if self._messages_win is None or not self._messages_win.winfo_exists():
            self._inbox_check_account_id = None
            return

        def fetch():
            return api_request(MESSAGING_URL, f"/inbox/check-status?account_id={account_id}", timeout=15.0)

        def done(result):
            if self._messages_win is None or not self._messages_win.winfo_exists():
                self._inbox_check_account_id = None
                return
            status = result["status"]
            totals = self._inbox_check_totals
            if status in ("queued", "running"):
                checked_so_far = totals["checked"] + result["checked"]
                self.inbox_check_status.configure(text=f"проверяю переписки в VK… {checked_so_far}")
                self.after(2000, lambda: self._poll_inbox_check(account_id))
                return

            if status == "failed":
                self._inbox_check_account_id = None
                self.inbox_check_status.configure(text="")
                messagebox.showerror("Входящие", result.get("error") or "не удалось проверить входящие")
                return

            totals["checked"] += result["checked"]
            totals["replied"] += result["replied"]
            if result.get("has_more"):
                # limit=20 за один батч (см. messaging-service/app/inbox.py) — в очереди на этот
                # аккаунт осталось больше, сами запускаем следующий батч, не заставляя нажимать
                # кнопку заново. Иначе пользователь видел бы "проверено 20" и решил бы, что это
                # всё, хотя реальных pending-сообщений может быть в разы больше (см. историю с
                # ЧЕБЕР — сообщение стояло в очереди за лимитом и не проверялось циклами).
                self.inbox_check_status.configure(text=f"проверяю переписки в VK… {totals['checked']}, продолжаю…")
                self._start_inbox_check_batch(account_id)
                return

            self._inbox_check_account_id = None
            self.inbox_check_status.configure(
                text=f"проверено {totals['checked']}, новых ответов: {totals['replied']}"
            )
            self.flash_status(f"Входящие: проверено {totals['checked']}, новых ответов: {totals['replied']}")
            self._load_messages()

        def error(exc):
            self._inbox_check_account_id = None
            self.inbox_check_status.configure(text="")
            messagebox.showerror("Входящие", str(exc))

        self.run_bg(fetch, on_done=done, on_error=error, dedupe_key="inbox_check_status")

    # ---------- lifecycle ----------
    def on_stop_app(self) -> None:
        if not messagebox.askyesno(
            "Остановить приложение",
            "Остановить все сервисы (docker compose stop: data/parser/messaging/orchestrator "
            "+ postgres) и закрыть это окно?\n\nЗапущенные в фоне кампании прервутся.",
        ):
            return
        self.flash_status("Останавливаю сервисы (docker compose stop)…")

        def stop():
            return subprocess.run(
                ["docker", "compose", "stop"],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=120,
            )

        def done(result: subprocess.CompletedProcess) -> None:
            if result.returncode == 0:
                messagebox.showinfo("Остановлено", "Все сервисы остановлены.")
                self.destroy()
            else:
                messagebox.showerror("Ошибка", result.stderr or f"docker compose stop завершился с кодом {result.returncode}")
                self.flash_status("Не удалось остановить сервисы", is_err=True)

        def error(exc: Exception) -> None:
            messagebox.showerror("Ошибка", f"Не удалось выполнить docker compose stop: {exc}")
            self.flash_status("Не удалось остановить сервисы", is_err=True)

        self.run_bg(stop, on_done=done, on_error=error)


def prompt_login() -> bool:
    """Модальный экран входа. Возвращает True, если логин/пароль совпали с USERS,
    False — если окно закрыли крестиком, не залогинившись."""
    root = tk.Tk()
    root.title("VK Lead-Gen — вход")
    root.geometry("300x190")
    root.resizable(False, False)
    root.eval("tk::PlaceWindow . center")

    logged_in = {"value": False}

    frame = ttk.Frame(root, padding=20)
    frame.pack(fill="both", expand=True)
    frame.columnconfigure(1, weight=1)

    ttk.Label(frame, text="VK Lead-Gen", font=("Segoe UI", 12, "bold")).grid(row=0, column=0, columnspan=2, pady=(0, 12))

    ttk.Label(frame, text="Логин:").grid(row=1, column=0, sticky="w", pady=4)
    login_entry = ttk.Entry(frame)
    login_entry.grid(row=1, column=1, sticky="ew", pady=4)

    ttk.Label(frame, text="Пароль:").grid(row=2, column=0, sticky="w", pady=4)
    password_entry = ttk.Entry(frame, show="*")
    password_entry.grid(row=2, column=1, sticky="ew", pady=4)

    error_label = ttk.Label(frame, text="", foreground="#c0392b")
    error_label.grid(row=3, column=0, columnspan=2, pady=(4, 0))

    def try_login(_event: object = None) -> None:
        login = login_entry.get().strip()
        password = password_entry.get()
        if USERS.get(login) == password:
            logged_in["value"] = True
            root.destroy()
        else:
            error_label.configure(text="Неверный логин или пароль")
            password_entry.delete(0, "end")
            password_entry.focus_set()

    ttk.Button(frame, text="Войти", command=try_login).grid(row=4, column=0, columnspan=2, pady=(14, 0), sticky="ew")
    login_entry.bind("<Return>", try_login)
    password_entry.bind("<Return>", try_login)
    login_entry.focus_set()

    root.mainloop()
    return logged_in["value"]


if __name__ == "__main__":
    if prompt_login():
        App().mainloop()
