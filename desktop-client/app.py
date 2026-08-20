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
from pathlib import Path
from tkinter import messagebox, ttk

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

        self._build_layout()
        self._schedule_list_refresh()
        self.refresh_health()
        self.refresh_accounts()
        self.refresh_campaigns()

    # ---------- layout ----------
    def _build_layout(self) -> None:
        header = ttk.Frame(self, padding=(14, 10))
        header.pack(fill="x")
        ttk.Label(header, text="VK Lead-Gen — панель управления", font=("Segoe UI", 13, "bold")).pack(side="left")
        ttk.Button(header, text="Остановить приложение", style="Danger.TButton", command=self.on_stop_app).pack(side="right", padx=(12, 0))
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

        cols = ("login", "purpose", "status", "session", "usage")
        self.acc_tree = ttk.Treeview(panel, columns=cols, show="headings", height=6)
        for col, title, width in (
            ("login", "Логин", 130),
            ("purpose", "Назначение", 100),
            ("status", "Статус", 90),
            ("session", "Вход в VK", 90),
            ("usage", "Нагрузка", 130),
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
        ttk.Button(btns, text="Обновить", command=self.refresh_detail).pack(side="left", padx=2)

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
    def run_bg(self, fn, on_done=None, on_error=None) -> None:
        def worker() -> None:
            try:
                result = fn()
            except Exception as exc:  # noqa: BLE001 - показываем любую ошибку пользователю
                if on_error:
                    self.after(0, lambda: on_error(exc))
                return
            if on_done:
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

        self.run_bg(check_all, on_done=done)

    # ---------- accounts ----------
    def refresh_accounts(self) -> None:
        def fetch():
            return api_request(DATA_URL, "/accounts")

        def done(accounts):
            self.acc_tree.delete(*self.acc_tree.get_children())
            for a in accounts:
                usage = f"{a['hourly_used']}/{a['hourly_limit']} ч · {a['daily_used']}/{a['daily_limit']} сут"
                has_session = a.get("has_session", False)
                session_text = "есть" if has_session else "нет входа"
                self.acc_tree.insert(
                    "", "end", iid=a["id"],
                    values=(a["login"], RU_PURPOSE.get(a["purpose"], a["purpose"]), RU_ACCOUNT_STATUS.get(a["status"], a["status"]), session_text, usage),
                    tags=("has_session" if has_session else "no_session",),
                )

        def error(exc):
            self.flash_status(f"Не удалось загрузить аккаунты: {exc}", is_err=True)

        self.run_bg(fetch, on_done=done, on_error=error)

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

        self.run_bg(fetch, on_done=done, on_error=error)

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
            self.detail_status.configure(text=status_text, foreground="#c0392b" if data.get("error") else "#555")
            self._update_progress(data.get("phase"), data.get("progress"))

        def error(exc):
            self.detail_status.configure(text=f"Ошибка загрузки статуса: {exc}", foreground="#c0392b")

        self.run_bg(fetch, on_done=done, on_error=error)
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

        self.run_bg(fetch, on_done=done)

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

        self.run_bg(fetch, on_done=done)

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


if __name__ == "__main__":
    App().mainloop()
