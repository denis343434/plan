"""Единая точка запуска всего стека одной командой/кнопкой Run в IDE.

Гибридный режим (см. docker-compose.override.yml): postgres, data-service,
orchestrator-service — в Docker; parser-service и messaging-service — локально через
свои venv (нужно для видимого Playwright, у Linux-контейнеров нет дисплея). Плюс
десктоп-GUI (Tkinter), который просто подключается к уже поднятым портам.

Запуск:  python run.py
Повторный запуск безопасен — уже отвечающие сервисы не трогает.
"""

import subprocess
import sys
import time
import urllib.request
from pathlib import Path

try:  # чтобы кириллица не превращалась в кракозябры в терминалах с не-UTF8 кодировкой вывода
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent

LOCAL_SERVICES = {
    "parser-service": 8002,
    "messaging-service": 8003,
}
DOCKER_HEALTH_PORTS = {
    "data-service": 8001,
    "orchestrator-service": 8004,
}
DOCKER_DESKTOP_EXE = Path(r"C:\Program Files\Docker\Docker\Docker Desktop.exe")


def _health_ok(port: int, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/health", timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def _wait_for(port: int, seconds: int = 60) -> bool:
    for _ in range(seconds):
        if _health_ok(port):
            return True
        time.sleep(1)
    return False


def _docker_ready() -> bool:
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def _ensure_docker_running() -> None:
    if _docker_ready():
        return
    print("Docker не запущен — стартую Docker Desktop...")
    if sys.platform == "win32" and DOCKER_DESKTOP_EXE.exists():
        subprocess.Popen([str(DOCKER_DESKTOP_EXE)])
    else:
        print("Не нашёл Docker Desktop по стандартному пути — запусти его вручную.")
    for _ in range(60):
        if _docker_ready():
            return
        time.sleep(2)
    raise RuntimeError("Docker не поднялся за 120с — проверь Docker Desktop вручную")


def _start_docker_stack() -> None:
    _ensure_docker_running()
    print("== Docker: postgres, data-service, orchestrator-service ==")
    subprocess.run(["docker", "compose", "up", "--build", "-d"], cwd=ROOT, check=True)
    if (ROOT / "docker-compose.override.yml").exists():
        # Гибридный режим — parser/messaging-service запускаются локально ниже, их
        # контейнеры (остаются описанными, т.к. orchestrator ссылается на них через
        # depends_on) держат порты 8002/8003 и должны быть остановлены.
        subprocess.run(
            ["docker", "compose", "stop", "parser-service", "messaging-service"],
            cwd=ROOT, check=True,
        )
    for name, port in DOCKER_HEALTH_PORTS.items():
        if not _wait_for(port):
            print(f"! {name} не ответил за 60с — смотри `docker compose logs {name}`")


def _python_for(service: str) -> Path:
    venv_python = ROOT / service / ".venv" / "Scripts" / "python.exe"
    return venv_python if venv_python.exists() else Path(sys.executable)


def _start_local_services() -> None:
    print("== Локально (venv): parser-service, messaging-service ==")
    for service, port in LOCAL_SERVICES.items():
        if _health_ok(port):
            print(f"{service} уже отвечает на {port} — пропускаю")
            continue
        log_path = ROOT / f"{service}.local.log"
        with open(log_path, "wb") as log_file:
            subprocess.Popen(
                [str(_python_for(service)), "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", str(port)],
                cwd=ROOT / service,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                # Своя (скрытая) консоль, а не унаследованная от run.py: после выхода run.py /
                # закрытия его терминала драйвер Playwright (node.exe), который сервис запускает
                # позже, падал сразу при старте — "Connection closed while reading from the
                # driver" → "'PlaywrightContextManager' object has no attribute '_playwright'"
                # (поймано вживую 2026-10-03 на парсинге и проверке сессий).
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
    for service, port in LOCAL_SERVICES.items():
        if not _wait_for(port):
            print(f"! {service} не поднялся за 60с — смотри {service}.local.log")


def _gui_already_running() -> bool:
    # У Tkinter-окна нет /health — проверяем через список процессов (Windows-only, best effort).
    if sys.platform != "win32":
        return False
    try:
        result = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                "Where-Object { $_.CommandLine -match 'desktop-client[\\\\/]app\\.py' } | "
                "Measure-Object).Count",
            ],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip() not in ("", "0")
    except Exception:
        return False


def _start_gui() -> None:
    print("== GUI ==")
    if _gui_already_running():
        print("desktop-client уже открыт — пропускаю")
        return
    log_path = ROOT / "desktop-client.log"
    with open(log_path, "wb") as log_file:
        subprocess.Popen(
            [sys.executable, "desktop-client/app.py"],
            cwd=ROOT,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            close_fds=False,
        )


def main() -> None:
    _start_docker_stack()
    _start_local_services()
    _start_gui()
    print()
    print("Готово:")
    print("  data-service          http://localhost:8001")
    print("  parser-service        http://localhost:8002")
    print("  messaging-service     http://localhost:8003")
    print("  orchestrator-service  http://localhost:8004")


if __name__ == "__main__":
    main()
