import time
from typing import Any

import httpx

from app.config import settings
from app.exceptions import DataServiceError, DataServiceNotFoundError, NoAccountAvailableError

_RETRYABLE_EXCEPTIONS = (httpx.ConnectError, httpx.TimeoutException)


class DataServiceClient:
    def __init__(self, base_url: str | None = None, timeout: float = 10.0) -> None:
        self._client = httpx.Client(base_url=base_url or settings.DATA_SERVICE_URL, timeout=timeout)

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
                raise DataServiceError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise DataServiceError(str(exc)) from exc

        if response.status_code == 404:
            raise DataServiceNotFoundError(response.text)
        if response.status_code == 409:
            raise NoAccountAvailableError(response.text)
        if response.is_error:
            raise DataServiceError(f"{response.status_code}: {response.text}")
        return response

    def bulk_insert_leads(self, leads: list[dict]) -> dict:
        return self._request("POST", "/leads/bulk", json=leads).json()

    def next_available_account(
        self,
        platform: str,
        purpose: str,
        lock_ttl_seconds: int = 900,
        task_ref: str | None = None,
    ) -> dict:
        body = {
            "platform": platform,
            "purpose": purpose,
            "lock_ttl_seconds": lock_ttl_seconds,
            "task_ref": task_ref,
        }
        return self._request("POST", "/accounts/next-available", json=body).json()

    def get_session(self, account_id: str) -> dict:
        return self._request("GET", f"/accounts/{account_id}/session").json()

    def release_account(self, account_id: str) -> dict:
        return self._request("POST", f"/accounts/{account_id}/release").json()

    def cooldown_account(
        self, account_id: str, minutes: int, reason: str | None = None, permanent: bool = False
    ) -> dict:
        body = {"minutes": minutes, "permanent": permanent, "reason": reason}
        return self._request("POST", f"/accounts/{account_id}/cooldown", json=body).json()
