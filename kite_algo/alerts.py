"""Kite Alerts API — raw HTTP client.

The Alerts API was added server-side on 2025-06-12 but, as of pykiteconnect
v5.1.0 (March 2026), the SDK does NOT wrap it (see open issue #220). We
implement it against the documented endpoints directly:

  GET    /alerts                  → list (?status, ?page, ?page_size)
  GET    /alerts/:uuid            → single
  POST   /alerts                  → create
  PUT    /alerts/:uuid            → modify
  DELETE /alerts?uuid=:uuid       → delete
  GET    /alerts/:uuid/history    → trigger history

Alert types:
- `simple`   — price/volume alert. Triggers notify the Kite app UI; for
               API consumers the only record is GET /alerts/:uuid/history.
- `ato`      — "Alert-Triggers-Order" — when the condition fires, Kite
               auto-places the `basket` (a JSON-encoded list of order specs).

Limits (as of research snapshot): 500 active alerts per user.

Rate limiting: routed through the `general` bucket (10 req/s) via a helper
that calls `KiteRateLimiter.wait_general()` before every HTTP call.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode


KITE_API_BASE = "https://api.kite.trade"
ALERTS_MAX_ACTIVE_PER_USER = 500


class AlertsAPIError(RuntimeError):
    """Raised when the Alerts endpoint returns a non-2xx or Kite error envelope."""
    def __init__(self, status_code: int, message: str, error_type: str | None = None,
                 request_id: str | None = None):
        self.status_code = status_code
        self.error_type = error_type
        self.request_id = request_id
        super().__init__(message)


def _headers(api_key: str, access_token: str) -> dict[str, str]:
    """Required headers for every Kite API call."""
    return {
        "X-Kite-Version": "3",
        "Authorization": f"token {api_key}:{access_token}",
    }


def _parse_response(resp: Any) -> Any:
    """Parse a Kite-shape response. Every endpoint returns either:
        {"status": "success", "data": ...}    on 2xx, or
        {"status": "error", "message": "...", "error_type": "..."}

    Raises AlertsAPIError on non-success.
    """
    status_code = resp.status_code
    request_id = resp.headers.get("x-kite-request-id") if hasattr(resp, "headers") else None
    try:
        body = resp.json()
    except (ValueError, AttributeError):
        raise AlertsAPIError(
            status_code=status_code,
            message=f"non-JSON response from alerts API (HTTP {status_code})",
            request_id=request_id,
        )

    if status_code >= 400 or body.get("status") == "error":
        raise AlertsAPIError(
            status_code=status_code,
            message=str(body.get("message") or f"HTTP {status_code}"),
            error_type=body.get("error_type"),
            request_id=request_id,
        )
    return body.get("data")


class AlertsClient:
    """Low-dependency Alerts API client. Uses `requests` for HTTP, pre-rate-
    limited via an external `KiteRateLimiter` passed into the constructor.
    """

    def __init__(
        self, api_key: str, access_token: str,
        *, rate_limiter: Any | None = None,
        base_url: str = KITE_API_BASE,
        http_session: Any | None = None,   # inject for testability
    ):
        self.api_key = api_key
        self.access_token = access_token
        self.base_url = base_url
        self._limiter = rate_limiter
        if http_session is None:
            import requests
            http_session = requests.Session()
        self._http = http_session

    def _wait(self) -> None:
        if self._limiter is not None:
            self._limiter.wait_general()

    def _req(self, method: str, path: str, **kwargs) -> Any:
        self._wait()
        url = f"{self.base_url}{path}"
        resp = self._http.request(
            method, url,
            headers=_headers(self.api_key, self.access_token),
            timeout=kwargs.pop("timeout", 10),
            **kwargs,
        )
        return _parse_response(resp)

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    def list(self, *, status: str | None = None, page: int = 1,
             page_size: int = 50) -> list[dict]:
        q = {"page": page, "page_size": page_size}
        if status:
            q["status"] = status
        return self._req("GET", f"/alerts?{urlencode(q)}")

    def get(self, uuid: str) -> dict:
        return self._req("GET", f"/alerts/{uuid}")

    def create(self, payload: dict) -> dict:
        # POST /alerts with form-encoded body per Kite convention. The
        # `basket` field for ATO alerts is a JSON-encoded string.
        if "basket" in payload and not isinstance(payload["basket"], str):
            payload = dict(payload)
            payload["basket"] = json.dumps(payload["basket"], default=str)
        return self._req("POST", "/alerts", data=payload)

    def modify(self, uuid: str, payload: dict) -> dict:
        if "basket" in payload and not isinstance(payload["basket"], str):
            payload = dict(payload)
            payload["basket"] = json.dumps(payload["basket"], default=str)
        return self._req("PUT", f"/alerts/{uuid}", data=payload)

    def delete(self, uuid: str) -> Any:
        return self._req("DELETE", f"/alerts?{urlencode({'uuid': uuid})}")

    def history(self, uuid: str) -> list[dict]:
        return self._req("GET", f"/alerts/{uuid}/history")
