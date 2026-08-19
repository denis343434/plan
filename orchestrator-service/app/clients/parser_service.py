import time
from typing import Any
from uuid import UUID

import httpx

from app.config import settings
from app.exceptions import ParserServiceError

_RETRYABLE_EXCEPTIONS = (httpx.ConnectError, httpx.TimeoutException)


class ParserServiceClient:
    def __init__(self, base_url: str | None = None, timeout: float = 10.0) -> None:
        self._client = httpx.Client(base_url=base_url or settings.PARSER_SERVICE_URL, timeout=timeout)

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
                raise ParserServiceError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise ParserServiceError(str(exc)) from exc

        if response.is_error:
            raise ParserServiceError(f"{response.status_code}: {response.text}")
        return response

    def start_parse(
        self,
        platform: str,
        keyword: str,
        campaign_id: UUID | str | None = None,
        filters: dict | None = None,
    ) -> dict:
        body = {"platform": platform, "keyword": keyword, "filters": filters or {}}
        if campaign_id is not None:
            body["campaign_id"] = str(campaign_id)
        return self._request("POST", "/parse", json=body).json()

    def get_parse_status(self, task_id: str) -> dict:
        return self._request("GET", f"/parse/{task_id}/status").json()
