from urllib.parse import unquote, urlsplit


def parse_proxy_url(raw: str, setting_name: str) -> dict | None:
    """"http://login:pass@host:port" → proxy-аргумент chromium.launch (None — без прокси).
    Логин/пароль Playwright принимает только отдельными полями, не внутри server-URL.
    Общий для VK_PROXY и TG_PROXY."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = f"http://{raw}"
    parts = urlsplit(raw)
    if not parts.hostname or not parts.port:
        raise ValueError(f"{setting_name} must look like http://login:pass@host:port, got {parts.hostname!r}")
    proxy = {"server": f"{parts.scheme}://{parts.hostname}:{parts.port}"}
    if parts.username:
        proxy["username"] = unquote(parts.username)
    if parts.password:
        proxy["password"] = unquote(parts.password)
    return proxy
