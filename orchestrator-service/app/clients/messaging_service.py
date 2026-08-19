import time
from typing import Any
from uuid import UUID

import httpx

from app.config import settings
from app.exceptions import MessagingServiceError

_RETRYABLE_EXCEPTIONS = (httpx.ConnectError, httpx.TimeoutException)


class MessagingServiceClient:
    def __init__(self, base_url: str | None = None, timeout: float = 10.0) -> None:
        self._client = httpx.Client(base_url=base_url or settings.MESSAGING_SERVICE_URL, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._client.request(method, url, **kwargs)
        except _RETRYABLE_EXCEPTIONS:
            time.sleep(0.5)
            try:
                response = self._client.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                raise MessagingServiceError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise MessagingServiceError(str(exc)) from exc

        if response.is_error:
            raise MessagingServiceError(f"{response.status_code}: {response.text}")
        return response

    def start_send(self, campaign_id: UUID | str) -> dict:
        return self._request("POST", f"/campaigns/{campaign_id}/send").json()

    def get_send_status(self, campaign_id: UUID | str) -> dict:
        return self._request("GET", f"/campaigns/{campaign_id}/send-status").json()
