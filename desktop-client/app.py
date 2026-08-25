"""
Десктоп-панель управления VK Lead-Gen (Tkinter, только стандартная библиотека).

Не отдельный микросервис — просто клиент, который ходит по HTTP к уже
запущенному `docker compose` стеку (Data/Parser/Messaging/Orchestrator на
localhost:8001-8004). Запуск: python app.py (из этой папки), при поднятом
`docker compose up` в корне репозитория.
"""

import base64
import json
import mimetypes
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
from tkinter import filedialog, messagebox, scrolledtext, ttk

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

# Держим в шаге с MAX_IMAGE_BYTES в messaging-service/app/schemas/reply.py — проверяем на
# клиенте до отправки, чтобы не гонять base64 через сеть только затем, чтобы сервер его отклонил.
MAX_REPLY_IMAGE_BYTES = 15 * 1024 * 1024

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
RU_DELIVERY_STATUS = {"sent": "отправлено", "failed": "ошибка", "pending": "сбой сети, повтор"}
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
        # account_id -> результат последней РЕАЛЬНОЙ проверки VK-сессии через Playwright
        # (см. on_check_sessions/GET /accounts/{id}/session-check у parser-service) — в отличие
        # от has_session (просто "сохраняли ли когда-то storage_state"), это фактическое "жива ли
        # она сейчас". Живёт только в памяти клиента поверх обычного refresh_accounts (тик каждые
        # REFRESH_LIST_MS), пока пользователь не перепроверит или не перелогинит аккаунт заново.
        self._session_check_cache: dict[str, bool] = {}
        self._session_check_pending = 0
        # Окно "Кому написали" — держим ссылку, чтобы повторный клик по кнопке в шапке
        # поднимал уже открытое окно, а не плодил дубликаты.
        self._messages_win: tk.Toplevel | None = None
        self._messages_tree: ttk.Treeview | None = None
        self._message_reply_text: scrolledtext.ScrolledText | None = None
        self._reply_compose_text: tk.Text | None = None
        self.reply_send_button: ttk.Button | None = None
        self._errors_tree: ttk.Treeview | None = None
        self._retry_tree: ttk.Treeview | None = None
        self._replies_tree: ttk.Treeview | None = None
        self._messages_notebook: ttk.Notebook | None = None
        self._messages_error_tab: ttk.Frame | None = None
        self._messages_retry_tab: ttk.Frame | None = None
        self._messages_replies_tab: ttk.Frame | None = None
        # message_id -> ссылка на группу (сама модель Message в Data Service ссылку не хранит,
        # только lead_id — подтягиваем её через join с /leads на клиенте, см. _load_messages).
        self._message_links: dict[str, str] = {}
        # lead_id -> все сообщения этого лида (все аккаунты/кампании разом) — по выбору строки
        # рисуем под таблицей мини-чат: своё сообщение + его ответ, друг за другом по sent_at
        # (см. _render_lead_chat/_load_messages).
        self._messages_by_lead: dict[str, list[dict]] = {}
        # message_id -> {"lead_id", "account_id"} — нужно кнопке "Ответить": отвечать обязаны с
        # того же аккаунта, что вёл переписку с этой группой (см. on_send_reply).
        self._message_reply_targets: dict[str, dict] = {}
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
        # campaign_id, для которой сейчас идёт прямая рассылка мимо Orchestrator — новым лидам
        # ("Запустить рассылку") или включая прежде проваленных ("Повторить с ошибками"), см.
        # _start_direct_send/_poll_retry_send — None, когда не запущена; не даёт запустить
        # вторую поверх идущей.
        self._retry_send_campaign_id: str | None = None
        # message_id, для которого сейчас отправляется ручной ответ — None, когда не идёт
        # отправка; не даёт запустить вторую поверх идущей (см. on_send_reply).
        self._reply_send_message_id: str | None = None
        # Путь к картинке, выбранной для прикрепления к следующему ручному ответу — None, если
        # ничего не выбрано (см. on_pick_reply_attachment/on_send_reply).
        self._reply_attachment_path: str | None = None
        self.reply_attachment_label: ttk.Label | None = None

        self._install_clipboard_shortcuts()
        self._build_layout()
        self._schedule_list_refresh()
        self.refresh_health()
        self.refresh_headless_mode()
        self.refresh_accounts()
        self.refresh_campaigns()
        self._tick_account_cooldowns()

    def _install_clipboard_shortcuts(self) -> None:
        # Стандартные Tk-биндинги <<Paste>>/<<Copy>>/<<Cut>> завязаны на keysym "v"/"c"/"x" —
        # на русской раскладке Windows физическая клавиша V/C/X даёт другой keysym, и Ctrl+V
        # молча ничего не делает ни в одном Entry/Text приложения. keycode — это код физической
        # клавиши, он не зависит от раскладки, поэтому дублируем действия через него.
        actions = {86: "<<Paste>>", 67: "<<Copy>>", 88: "<<Cut>>", 65: "<<SelectAll>>"}
        # keysym для этих же действий на английской раскладке — там штатный Tk-биндинг класса
        # Entry/Text и так сработает сам. Раньше это не проверялось, и на английской раскладке
        # вставка срабатывала ДВАЖДЫ — сначала штатный биндинг класса (bindtag выше, чем bind_all
        # ниже), потом ещё раз наш keycode-биндинг поверх него — текст при вставке дублировался
        # (подтверждено пользователем 2026-08-24). Вмешиваемся только когда keysym НЕ совпадает
        # со штатным (т.е. раскладка не английская и штатный биндинг молчит).
        _standard_keysyms = {"<<Paste>>": "v", "<<Copy>>": "c", "<<Cut>>": "x", "<<SelectAll>>": "a"}

        def on_control_key(event: tk.Event) -> str | None:
            action = actions.get(event.keycode)
            if action is None or not (event.state & 0x4):
                return None
            if event.keysym.lower() == _standard_keysyms[action]:
                return None  # штатный Tk-биндинг уже обработает сам — не дублируем
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
        self._build_headless_toggle(header).pack(side="right", padx=(12, 0))
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

    def _build_headless_toggle(self, parent: ttk.Frame) -> ttk.Frame:
        frame = ttk.Frame(parent)
        ttk.Label(frame, text="Playwright:", font=("Segoe UI", 9)).pack(side="left", padx=(0, 4))
        self.headless_mode = tk.StringVar(value="headless")
        ttk.Radiobutton(
            frame, text="В фоне", value="headless", variable=self.headless_mode, command=self.on_headless_mode_change
        ).pack(side="left")
        ttk.Radiobutton(
            frame, text="На экране", value="headed", variable=self.headless_mode, command=self.on_headless_mode_change
        ).pack(side="left")
        return frame

    def refresh_headless_mode(self) -> None:
        # Источник истины — parser-service (оба сервиса переключаются вместе через
        # on_headless_mode_change, так что после старта приложения они не должны расходиться;
        # если всё же разошлись, например, из-за прямого запроса к API мимо панели — покажем
        # тот режим, что видит parser-service).
        def fetch():
            return api_request(PARSER_URL, "/config/headless")

        def done(result: dict | None) -> None:
            if result is not None:
                self.headless_mode.set("headed" if result.get("headless") is False else "headless")

        self.run_bg(fetch, on_done=done)

    def on_headless_mode_change(self) -> None:
        headless = self.headless_mode.get() == "headless"
        mode_label = "в фоне" if headless else "на экране"

        def push():
            errors = []
            for base in (PARSER_URL, MESSAGING_URL):
                try:
                    api_request(base, "/config/headless", method="PUT", body={"headless": headless})
                except ApiError as exc:
                    errors.append(str(exc))
            if errors:
                raise ApiError("; ".join(errors))

        def done(_) -> None:
            self.flash_status(f"Режим Playwright для новых запусков: {mode_label}")

        def error(exc) -> None:
            self.flash_status(f"Не удалось применить режим Playwright ({mode_label}): {exc}", is_err=True)

        self.run_bg(push, on_done=done, on_error=error)

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
        self.acc_hourly.insert(0, "60")
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
        self.acc_tree.tag_configure("checking_session", foreground="#888")
        self.acc_tree.pack(fill="both", expand=True)

        acc_actions = ttk.Frame(panel)
        acc_actions.pack(fill="x", pady=(6, 0))
        ttk.Button(acc_actions, text="Тестовая сессия для выбранного", command=self.on_set_session).pack(side="left")
        ttk.Button(acc_actions, text="Войти в VK", command=self.on_vk_login).pack(side="left", padx=(6, 0))
        ttk.Button(acc_actions, text="Проверить сессии", command=self.on_check_sessions).pack(side="left", padx=(6, 0))
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
        ttk.Button(btns, text="Запустить рассылку", command=self.on_start_new_leads_send).pack(side="left", padx=2)
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
        ttk.Button(tpl_form, text="Удалить шаблон", style="Danger.TButton", command=self.on_delete_template).pack(
            side="left", padx=(6, 0)
        )

        tpl_cols = ("variant", "body")
        self.tpl_tree = ttk.Treeview(panel, columns=tpl_cols, show="headings", height=3)
        for col, title, width in (("variant", "Вариант", 70), ("body", "Текст", 900)):
            self.tpl_tree.heading(col, text=title)
            self.tpl_tree.column(col, width=width, anchor="w")
        self.tpl_tree.pack(fill="x", pady=(0, 10))
        # Клик по строке подставляет её вариант/текст в форму выше — удобно и для правки перед
        # "Сохранить шаблон" (тот же вариант обновит именно эту запись, см. on_save_template в
        # data-service — upsert по campaign_id+variant), и чтобы точно понимать, что удалишь.
        self.tpl_tree.bind("<<TreeviewSelect>>", self._on_template_row_selected)

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
                # Реальный результат "Проверить сессии" (см. on_check_sessions) перекрывает
                # дефолтный "есть/нет входа" по has_session — тот отвечает только "сохраняли ли
                # когда-то storage_state", а не жива ли сессия сейчас (см. _session_check_cache).
                checked = self._session_check_cache.get(a["id"]) if has_session else None
                if checked is True:
                    session_text, session_tag = "жива", "has_session"
                elif checked is False:
                    session_text, session_tag = "истекла", "no_session"
                else:
                    session_text = "есть" if has_session else "нет входа"
                    session_tag = "has_session" if has_session else "no_session"
                self.acc_tree.insert(
                    "", "end", iid=a["id"],
                    values=(
                        a["login"], RU_PURPOSE.get(a["purpose"], a["purpose"]),
                        RU_ACCOUNT_STATUS.get(a["status"], a["status"]), session_text, usage,
                        self._cooldown_text(a),
                    ),
                    tags=(session_tag,),
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
            "hourly_limit": int(self._value(self.acc_hourly) or 60),
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

    def _set_session_cell(self, account_id: str, text: str, tag: str) -> None:
        if not self.acc_tree.exists(account_id):
            return
        values = list(self.acc_tree.item(account_id, "values"))
        values[3] = text  # колонка "session" — см. cols в _build_accounts_panel
        self.acc_tree.item(account_id, values=values, tags=(tag,))

    def on_check_sessions(self) -> None:
        # Проверяем реально, только если хоть какая-то сессия сохранена (has_session) — без
        # неё и так очевидно "нет входа", ходить в parser-service незачем (см. GET
        # /accounts/{id}/session-check). При VK_HEADLESS=false на каждый аккаунт открывается
        # своё окно Chromium — это ожидаемо и есть весь смысл кнопки.
        targets = [a for a in self._accounts_cache.values() if a.get("has_session")]
        if not targets:
            self.flash_status("Ни у одного аккаунта нет сохранённой сессии — нечего проверять")
            return
        if self._session_check_pending:
            self.flash_status("Проверка сессий уже идёт…", is_err=True)
            return

        self._session_check_pending = len(targets)
        for a in targets:
            self._set_session_cell(a["id"], "проверяю…", "checking_session")
        self.flash_status(f"Проверяю сессии ({len(targets)})… может открыться окно Chromium на аккаунт")

        for a in targets:
            account_id = a["id"]

            def fetch(account_id=account_id):
                return api_request(PARSER_URL, f"/accounts/{account_id}/session-check", timeout=25.0)

            def done(result, account_id=account_id):
                valid = bool(result and result.get("valid"))
                self._session_check_cache[account_id] = valid
                self._set_session_cell(
                    account_id, "жива" if valid else "истекла", "has_session" if valid else "no_session"
                )
                self._session_check_pending -= 1
                if self._session_check_pending <= 0:
                    self.flash_status("Проверка сессий завершена")

            def error(exc, account_id=account_id):
                self._session_check_cache.pop(account_id, None)
                self._set_session_cell(account_id, "ошибка проверки", "no_session")
                self._session_check_pending -= 1
                if self._session_check_pending <= 0:
                    self.flash_status(f"Проверка сессий завершена с ошибками: {exc}", is_err=True)

            self.run_bg(fetch, on_done=done, on_error=error)

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

    def on_start_new_leads_send(self) -> None:
        # Только отправка, без retry_failed — те же ещё не тронутые (status=new) лиды, что и
        # обычный запуск кампании подхватил бы на фазе рассылки, но без повторного парсинга и
        # без прежде проваленных лидов (см. _start_direct_send). Нужна отдельно от "Запустить":
        # если кампания встала на фазе рассылки (см. запрос пользователя 2026-08-25 — таймаут
        # оркестратора при живой рассылке), "Запустить" гонял бы парсинг заново вхолостую.
        self._start_direct_send(retry_failed=False)

    def on_retry_failed_send(self) -> None:
        self._start_direct_send(retry_failed=True)

    def _start_direct_send(self, retry_failed: bool) -> None:
        if not self.selected_campaign_id:
            return
        campaign_id = self.selected_campaign_id
        if self._retry_send_campaign_id is not None:
            messagebox.showinfo("Рассылка", "Рассылка уже идёт, дождитесь её окончания")
            return

        self._retry_send_campaign_id = campaign_id
        self.detail_progress_bar.pack_forget()
        self.detail_progress_label.configure(text="запускаю рассылку…")

        def start():
            # Напрямую в Messaging Service, в обход Orchestrator — "Запустить" гоняет весь цикл
            # заново (включая повторный парсинг), а тут нужно только (пере)отправить уже
            # найденным лидам, независимо от того, в каком состоянии застряла кампания у
            # Orchestrator (paused/timeout и т.п. — Messaging Service статус кампании не
            # проверяет, см. app/routers/send.py). POST /campaigns/{id}/send рассылает всем
            # лидам со status=new — туда попадают лиды, у которых прошлая отправка провалилась
            # без признаков флуда (см. messaging-service/app/tasks.py:_process_lead — такие лиды
            # НЕ переводятся в "contacted", остаются "new" именно для повторной отправки), плюс
            # любые ещё не тронутые лиды. retry_failed=true ("Повторить с ошибками") явно
            # включает уже провалившихся лидов обратно в выборку — retry_failed=false
            # ("Запустить рассылку") их пропускает, отправляя только новым/нетронутым лидам
            # (см. run_send_task, previously_failed_lead_ids).
            query = "?retry_failed=true" if retry_failed else ""
            return api_request(MESSAGING_URL, f"/campaigns/{campaign_id}/send{query}", method="POST")

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
                    text=f"Рассылка: отправлено {result['sent']}, ошибок {result['failed']}, пропущено {result['skipped']}"
                )
                self.after(2000, lambda: self._poll_retry_send(campaign_id))
                return

            self._retry_send_campaign_id = None
            if status == "failed":
                self.detail_progress_label.configure(text="")
                messagebox.showerror("Рассылка", result.get("error") or "не удалось запустить рассылку")
                return
            if status == "waiting_for_account":
                self.detail_progress_label.configure(
                    text=f"Рассылка приостановлена: нет свободного аккаунта "
                    f"(отправлено {result['sent']}, ошибок {result['failed']})"
                )
                self.flash_status("Рассылка приостановлена: нет свободного аккаунта")
                self.refresh_leads()
                return

            self.detail_progress_label.configure(
                text=f"Рассылка завершена: отправлено {result['sent']}, ошибок {result['failed']}, пропущено {result['skipped']}"
            )
            self.flash_status(f"Рассылка: отправлено {result['sent']}, ошибок {result['failed']}")
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
            self.tpl_tree.delete(*self.tpl_tree.get_children())
            own = [t for t in templates if t.get("campaign_id") == campaign_id]
            for t in sorted(own, key=lambda t: t["variant"]):
                self.tpl_tree.insert("", "end", iid=t["id"], values=(t["variant"], t["body"]))

        self.run_bg(fetch, on_done=done, dedupe_key="templates")

    def _on_template_row_selected(self, _event: object = None) -> None:
        selected = self.tpl_tree.selection()
        if not selected:
            return
        variant, body = self.tpl_tree.item(selected[0], "values")
        self.tpl_variant.set(variant)
        self.tpl_body.delete(0, "end")
        self.tpl_body.insert(0, body)
        self.tpl_body.configure(foreground="black")

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
            # data-service делает upsert по (campaign_id, variant) — повторное сохранение того
            # же варианта обновляет существующую запись, а не плодит дубликат (см. crud/
            # templates.py::create_template).
            return api_request(DATA_URL, "/templates", method="POST", body=payload)

        def done(_result):
            self.flash_status("Шаблон сохранён")
            self.refresh_templates()

        def error(exc):
            messagebox.showerror("Ошибка", str(exc))

        self.run_bg(save, on_done=done, on_error=error)

    def on_delete_template(self) -> None:
        selected = self.tpl_tree.selection()
        if not selected:
            messagebox.showinfo("Шаблон", "Сначала выберите шаблон в таблице")
            return
        template_id = selected[0]
        variant, body = self.tpl_tree.item(template_id, "values")
        if not messagebox.askyesno("Удалить шаблон", f'Удалить вариант {variant} — "{body}"?'):
            return

        def delete():
            return api_request(DATA_URL, f"/templates/{template_id}", method="DELETE")

        def done(_result):
            self.flash_status("Шаблон удалён")
            self.refresh_templates()

        def error(exc):
            messagebox.showerror("Ошибка", str(exc))

        self.run_bg(delete, on_done=done, on_error=error)

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
        retry_tab = ttk.Frame(notebook)
        replies_tab = ttk.Frame(notebook)
        notebook.add(all_tab, text="Отправленные")
        notebook.add(error_tab, text="Ошибки")
        notebook.add(retry_tab, text="Повторно")
        notebook.add(replies_tab, text="Ответили")
        self._messages_notebook = notebook
        self._messages_error_tab = error_tab
        self._messages_retry_tab = retry_tab
        self._messages_replies_tab = replies_tab

        self._messages_tree = self._build_messages_tree(all_tab)
        self._errors_tree = self._build_messages_tree(error_tab)
        self._retry_tree = self._build_messages_tree(retry_tab)
        self._replies_tree = self._build_messages_tree(replies_tab)

        reply_frame = ttk.Frame(win, padding=(10, 0, 10, 10))
        reply_frame.pack(fill="x")
        ttk.Label(reply_frame, text="Переписка с этим лидом:").pack(anchor="w")
        reply_text = scrolledtext.ScrolledText(reply_frame, height=10, wrap="word", background="#fafafa")
        reply_text.configure(state="disabled")
        reply_text.pack(fill="x", pady=(2, 0))
        # Имитация чата VK: наши сообщения — справа на голубом, ответы лида — слева на сером.
        # Text не рисует настоящие "пузыри" со скруглением — background на всю ширину строки
        # плюс отступы полями (lmargin/rmargin) это приемлемо имитируют для десктоп-таблицы.
        reply_text.tag_configure(
            "out_bubble", justify="right", background="#dbeafe",
            lmargin1=60, lmargin2=60, rmargin=8, spacing1=1, spacing3=8,
        )
        reply_text.tag_configure(
            "in_bubble", justify="left", background="#e9e9e9",
            lmargin1=8, lmargin2=8, rmargin=60, spacing1=1, spacing3=8,
        )
        reply_text.tag_configure("out_meta", justify="right", foreground="#888", font=("Segoe UI", 8), rmargin=8)
        reply_text.tag_configure("in_meta", justify="left", foreground="#888", font=("Segoe UI", 8), lmargin1=8)
        self._message_reply_text = reply_text

        compose_frame = ttk.Frame(win, padding=(10, 0, 10, 10))
        compose_frame.pack(fill="x")
        ttk.Label(compose_frame, text="Ответить (с того же аккаунта):").pack(anchor="w")
        compose_row = ttk.Frame(compose_frame)
        compose_row.pack(fill="x", pady=(2, 0))
        compose_text = tk.Text(compose_row, height=3, wrap="word")
        compose_text.pack(side="left", fill="x", expand=True)
        self._reply_compose_text = compose_text
        self.reply_send_button = ttk.Button(
            compose_row, text="Ответить", style="Accent.TButton", command=self.on_send_reply
        )
        self.reply_send_button.pack(side="left", padx=(6, 0), anchor="n")

        attach_row = ttk.Frame(compose_frame)
        attach_row.pack(fill="x", pady=(4, 0))
        ttk.Button(attach_row, text="Прикрепить фото", command=self.on_pick_reply_attachment).pack(side="left")
        ttk.Button(attach_row, text="Убрать", command=self.on_clear_reply_attachment).pack(side="left", padx=(6, 0))
        self.reply_attachment_label = ttk.Label(attach_row, text="файл не выбран", foreground="#777")
        self.reply_attachment_label.pack(side="left", padx=(8, 0))

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
        tree.tag_configure("retry", foreground="#c9820a")
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
        if self._messages_retry_tab is not None and current == str(self._messages_retry_tab):
            return self._retry_tree
        if self._messages_replies_tab is not None and current == str(self._messages_replies_tab):
            return self._replies_tree
        return self._messages_tree

    def _load_messages(self) -> None:
        tree = self._messages_tree
        errors_tree = self._errors_tree
        retry_tree = self._retry_tree
        replies_tree = self._replies_tree
        if tree is None or errors_tree is None or retry_tree is None or replies_tree is None:
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
            retry_tree.delete(*retry_tree.get_children())
            replies_tree.delete(*replies_tree.get_children())
            self._message_links = {}
            self._message_reply_targets = {}
            self._messages_by_lead = {}
            for m in messages:
                self._messages_by_lead.setdefault(m["lead_id"], []).append(m)
            error_count = 0
            retry_count = 0
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
                # просмотрели (см. _on_message_row_selected); pending — сетевой сбой (плохой
                # интернет/страница не прогрузилась), не окончательная ошибка, см. tasks.py/
                # reply.py::network_error — отдельным цветом, чтобы не путать с "Ошибки".
                if reply_status == "replied" and m["id"] not in self._acknowledged_reply_ids:
                    tag = "replied"
                elif status == "pending":
                    tag = "retry"
                else:
                    tag = "sent" if status == "sent" else "failed"
                row_values = (
                    when, campaign_name, title, group_url, status_text,
                    reply_text, m.get("reply_preview") or "", m.get("error_reason") or "",
                )
                if status == "failed":
                    errors_tree.insert("", "end", iid=m["id"], values=row_values, tags=(tag,))
                    error_count += 1
                elif status == "pending":
                    retry_tree.insert("", "end", iid=m["id"], values=row_values, tags=(tag,))
                    retry_count += 1
                elif reply_status == "replied":
                    replies_tree.insert("", "end", iid=m["id"], values=row_values, tags=(tag,))
                    reply_count += 1
                else:
                    tree.insert("", "end", iid=m["id"], values=row_values, tags=(tag,))
                self._message_links[m["id"]] = group_url
                self._message_reply_targets[m["id"]] = {"lead_id": m["lead_id"], "account_id": m["account_id"]}

            if self._messages_notebook is not None and self._messages_error_tab is not None:
                self._messages_notebook.tab(self._messages_error_tab, text=f"Ошибки ({error_count})")
            if self._messages_notebook is not None and self._messages_retry_tab is not None:
                self._messages_notebook.tab(self._messages_retry_tab, text=f"Повторно ({retry_count})")
            if self._messages_notebook is not None and self._messages_replies_tab is not None:
                self._messages_notebook.tab(self._messages_replies_tab, text=f"Ответили ({reply_count})")
            self._on_message_row_selected(self._active_messages_tree())

        def error(exc):
            self.flash_status(f"Не удалось загрузить сообщения: {exc}", is_err=True)

        self.run_bg(fetch, on_done=done, on_error=error, dedupe_key="messages")

    def _on_message_row_selected(self, tree: ttk.Treeview | None = None) -> None:
        tree = tree if tree is not None else self._active_messages_tree()
        if tree is None:
            return
        selected = tree.selection()
        lead_id = None
        if selected:
            message_id = selected[0]
            if "replied" in tree.item(message_id, "tags"):
                self._acknowledged_reply_ids.add(message_id)
                tree.item(message_id, tags=("sent",))
            target = self._message_reply_targets.get(message_id)
            lead_id = target["lead_id"] if target else None
        self._render_lead_chat(lead_id)

    def _render_lead_chat(self, lead_id: str | None) -> None:
        """Мини-чат под таблицей — все сообщения этого лида (по всем аккаунтам/кампаниям)
        друг за другом по времени, наши справа/голубым, ответы лида слева/серым, как в VK."""
        chat = self._message_reply_text
        if chat is None or not chat.winfo_exists():
            return

        chat.configure(state="normal")
        chat.delete("1.0", "end")
        if not lead_id:
            chat.configure(state="disabled")
            return

        entries: list[tuple[str, str, str]] = []
        for m in self._messages_by_lead.get(lead_id, []):
            sent_at = (m.get("sent_at") or "")
            entries.append((sent_at, "out", m.get("text_sent") or ""))
            if m.get("reply_preview"):
                replied_at = m.get("replied_at") or sent_at
                entries.append((replied_at, "in", m["reply_preview"]))
        entries.sort(key=lambda e: e[0])

        for i, (ts, side, text) in enumerate(entries):
            if i > 0:
                chat.insert("end", "\n")
            label = "Мы" if side == "out" else "Лид"
            when = ts[:16].replace("T", " ")
            chat.insert("end", f"{label} · {when}\n", (f"{side}_meta",))
            chat.insert("end", f"{text}\n", (f"{side}_bubble",))

        chat.configure(state="disabled")

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

    def on_send_reply(self) -> None:
        tree = self._active_messages_tree()
        compose_text = self._reply_compose_text
        if tree is None or compose_text is None:
            return
        selected = tree.selection()
        if not selected:
            messagebox.showinfo("Ответить", "Сначала выберите строку в таблице")
            return
        message_id = selected[0]
        target = self._message_reply_targets.get(message_id)
        if not target:
            messagebox.showinfo("Ответить", "Для этой записи не найдены лид/аккаунт")
            return
        text = compose_text.get("1.0", "end").strip()
        attachment_path = self._reply_attachment_path
        if not text and not attachment_path:
            messagebox.showinfo("Ответить", "Введите текст ответа или прикрепите файл")
            return
        if self._reply_send_message_id is not None:
            messagebox.showinfo("Ответить", "Отправка ответа уже идёт, дождитесь её окончания")
            return
        if attachment_path and os.path.getsize(attachment_path) > MAX_REPLY_IMAGE_BYTES:
            messagebox.showerror("Ответить", "Файл слишком большой (максимум 15 МБ)")
            return

        self._reply_send_message_id = message_id
        self.reply_send_button.configure(state="disabled")
        self.flash_status("Отправляю ответ…")

        def start():
            body = {"account_id": target["account_id"], "text": text}
            if attachment_path:
                with open(attachment_path, "rb") as f:
                    body["image_base64"] = base64.b64encode(f.read()).decode("ascii")
                body["image_filename"] = os.path.basename(attachment_path)
            return api_request(
                MESSAGING_URL,
                f"/leads/{target['lead_id']}/reply",
                method="POST",
                body=body,
                timeout=90.0 if attachment_path else 45.0,
            )

        def done(result):
            self._reply_send_message_id = None
            if self.reply_send_button.winfo_exists():
                self.reply_send_button.configure(state="normal")
            if result.get("delivery_status") == "sent":
                if compose_text.winfo_exists():
                    compose_text.delete("1.0", "end")
                self._clear_reply_attachment()
                self.flash_status("Ответ отправлен")
            else:
                self.flash_status(
                    f"Не удалось отправить ответ: {result.get('error_reason') or 'неизвестная ошибка'}",
                    is_err=True,
                )
            self._load_messages()

        def error(exc):
            self._reply_send_message_id = None
            if self.reply_send_button.winfo_exists():
                self.reply_send_button.configure(state="normal")
            messagebox.showerror("Ответить", str(exc))

        self.run_bg(start, on_done=done, on_error=error)

    def on_pick_reply_attachment(self) -> None:
        path = filedialog.askopenfilename(
            title="Выбрать изображение",
            filetypes=[("Изображения", "*.png *.jpg *.jpeg *.gif *.webp"), ("Все файлы", "*.*")],
        )
        if not path:
            return
        self._reply_attachment_path = path
        if self.reply_attachment_label is not None:
            self.reply_attachment_label.configure(text=os.path.basename(path), foreground="black")

    def on_clear_reply_attachment(self) -> None:
        self._clear_reply_attachment()

    def _clear_reply_attachment(self) -> None:
        self._reply_attachment_path = None
        if self.reply_attachment_label is not None and self.reply_attachment_label.winfo_exists():
            self.reply_attachment_label.configure(text="файл не выбран", foreground="#777")

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
                if result.get("session_expired"):
                    # Не всплывающее окно — это известная, часто повторяющаяся ситуация
                    # (сессия аккаунта протухла), а не неожиданный сбой. Показываем прямо в
                    # приложении: статус сессии этого аккаунта в таблице аккаунтов (см.
                    # _session_check_cache/on_check_sessions) + текст в строке проверки —
                    # см. запрос пользователя 2026-08-24 "ошибка должна быть в приложении, а
                    # не окном винды".
                    self._session_check_cache[account_id] = False
                    self._set_session_cell(account_id, "истекла", "no_session")
                    self.inbox_check_status.configure(text="сессия VK истекла — см. колонку «Вход в VK» в таблице аккаунтов")
                    self.flash_status(f"Входящие: {result.get('error') or 'сессия VK истекла'}", is_err=True)
                else:
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
