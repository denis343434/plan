import time
from typing import Any
from uuid import UUID

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

    def list_leads(
        self,
        status: str | None = None,
        platform: str | None = None,
        campaign_id: UUID | str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if status is not None:
            params["status"] = status
        if platform is not None:
            params["platform"] = platform
        if campaign_id is not None:
            params["campaign_id"] = str(campaign_id)
        return self._request("GET", "/leads", params=params).json()

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

    def patch_lead_status(self, lead_id: str, status: str) -> dict:
        return self._request("PATCH", f"/leads/{lead_id}/status", json={"status": status}).json()

    def post_message(self, message: dict) -> dict:
        return self._request("POST", "/messages", json=message).json()

    def list_messages(self, account_id: str | None = None, lead_id: str | None = None) -> list[dict]:
        params: dict[str, Any] = {}
        if account_id is not None:
            params["account_id"] = account_id
        if lead_id is not None:
            params["lead_id"] = lead_id
        return self._request("GET", "/messages", params=params).json()

    def update_message_reply(self, message_id: str, reply_status: str, reply_preview: str | None = None) -> dict:
        body = {"reply_status": reply_status, "reply_preview": reply_preview}
        return self._request("PATCH", f"/messages/{message_id}/reply", json=body).json()

    def get_campaign(self, campaign_id: str) -> dict:
        return self._request("GET", f"/campaigns/{campaign_id}").json()

    def get_template(self, template_id: str) -> dict:
        return self._request("GET", f"/templates/{template_id}").json()

    def list_templates(self) -> list[dict]:
        return self._request("GET", "/templates").json()
