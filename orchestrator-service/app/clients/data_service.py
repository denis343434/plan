import time
from typing import Any
from uuid import UUID

import httpx

from app.config import settings
from app.exceptions import DataServiceError, DataServiceNotFoundError

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
        if response.is_error:
            raise DataServiceError(f"{response.status_code}: {response.text}")
        return response

    def create_campaign(self, payload: dict) -> dict:
        return self._request("POST", "/campaigns", json=payload).json()

    def get_campaign(self, campaign_id: UUID | str) -> dict:
        return self._request("GET", f"/campaigns/{campaign_id}").json()

    def update_campaign_status(self, campaign_id: UUID | str, status: str) -> dict:
        return self._request("PATCH", f"/campaigns/{campaign_id}", json={"status": status}).json()

    def list_leads(
        self, campaign_id: UUID | str, status: str, limit: int = 1000
    ) -> list[dict]:
        params = {"campaign_id": str(campaign_id), "status": status, "limit": limit}
        return self._request("GET", "/leads", params=params).json()

    def cooldown_account(
        self, account_id: UUID | str, minutes: int, reason: str | None = None, permanent: bool = False
    ) -> dict:
        body = {"minutes": minutes, "permanent": permanent, "reason": reason}
        return self._request("POST", f"/accounts/{account_id}/cooldown", json=body).json()
