import logging
from collections import OrderedDict
from typing import Callable

from app.adapters.base import SendAdapter
from app.clients.data_service import DataServiceClient
from app.config import settings

logger = logging.getLogger(__name__)


class SendAdapterPool:
    """Держит по одному открытому адаптеру (= браузеру) на аккаунт на всё время одного запуска
    рассылки — вместо запуска Chromium с нуля на каждого лида (см. PersistentBrowserMixin).

    Аккаунты выдаются data-service'ом на каждого лида заново (next_available_account), так что
    один и тот же аккаунт обычно возвращается подряд много раз — его браузер просто
    переиспользуется. Не больше SEND_MAX_OPEN_BROWSERS одновременно: при ротации большого пула
    самый давно неиспользованный закрывается.

    Только для одного потока — см. предупреждение о потоках в PersistentBrowserMixin.
    """

    def __init__(
        self, client: DataServiceClient, adapter_factory: Callable[[str, dict], SendAdapter]
    ) -> None:
        # adapter_factory(platform, storage_state) — обычно registry.get_adapter; передаётся
        # снаружи, чтобы вызывающий модуль (и тесты, подменяющие его get_adapter) решал сам.
        self._client = client
        self._adapter_factory = adapter_factory
        self._adapters: OrderedDict[str, SendAdapter] = OrderedDict()

    def get(self, account: dict, platform: str) -> SendAdapter:
        """Может бросить DataServiceError (нет сохранённой сессии) — как и раньше get_session в
        _process_lead. Сессия запрашивается только при первом использовании аккаунта."""
        key = f"{platform}:{account['id']}"
        adapter = self._adapters.get(key)
        if adapter is not None:
            self._adapters.move_to_end(key)
            return adapter

        session = self._client.get_session(account["id"])
        adapter = self._adapter_factory(platform, session["storage_state"])
        while len(self._adapters) >= max(1, settings.SEND_MAX_OPEN_BROWSERS):
            _, oldest = self._adapters.popitem(last=False)
            _close(oldest)
        open_ = getattr(adapter, "open", None)
        if open_ is not None:
            open_()
        self._adapters[key] = adapter
        return adapter

    def discard(self, account: dict, platform: str) -> None:
        """Аккаунт ушёл в cooldown (флуд/протухшая сессия) — его браузер больше не нужен, а после
        перелогина сессия будет другой."""
        adapter = self._adapters.pop(f"{platform}:{account['id']}", None)
        if adapter is not None:
            _close(adapter)

    def close(self) -> None:
        while self._adapters:
            _, adapter = self._adapters.popitem()
            _close(adapter)

    def __enter__(self) -> "SendAdapterPool":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def _close(adapter: SendAdapter) -> None:
    close = getattr(adapter, "close", None)  # у DryRunAdapter и тестовых фейков его нет
    if close is None:
        return
    try:
        close()
    except Exception:  # noqa: BLE001 - закрытие браузера не должно ронять рассылку
        logger.exception("failed to close send adapter")
