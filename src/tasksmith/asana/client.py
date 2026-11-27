"""Thin urllib JSON client with role checks and safe retries."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Literal

from tasksmith.asana.errors import AsanaAPIError, AsanaConfigError, AsanaRateLimitError

BASE = "https://app.asana.com/api/1.0"
_MAX_RETRIES = 3
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_INITIAL_BACKOFF_SEC = 1.0
_MAX_TIMEOUT_RETRIES = 2

ClientRole = Literal["writer", "reader"]
_VALID_ROLES = frozenset({"writer", "reader"})


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    return isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, TimeoutError)


class AsanaClient:
    """Authenticated Asana REST client."""

    def __init__(self, access_token: str, *, role: ClientRole = "reader"):
        if not access_token:
            raise AsanaConfigError("access token must not be empty")
        if role not in _VALID_ROLES:
            raise AsanaConfigError(f"invalid client role: {role!r}")
        self._token = access_token
        self._role = role

    @property
    def role(self) -> ClientRole:
        return self._role

    def request_json(
        self,
        method: str,
        path: str,
        *,
        query: str | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._role == "reader" and method.upper() != "GET":
            raise AsanaConfigError(f"reader client rejects non-GET method {method.upper()!r}")
        url = f"{BASE}{path}"
        if query:
            url = f"{url}?{query}"

        data_bytes: bytes | None = None
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }
        if body is not None:
            data_bytes = json.dumps({"data": body}).encode("utf-8")
            headers["Content-Type"] = "application/json"

        attempt = 0
        backoff = _INITIAL_BACKOFF_SEC
        timeout_attempt = 0
        timeout_backoff = _INITIAL_BACKOFF_SEC
        is_get = method.upper() == "GET"
        while True:
            req = urllib.request.Request(url, data=data_bytes, method=method.upper(), headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=60) as res:
                    raw = res.read().decode("utf-8")
                    if not raw.strip():
                        return {}
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError as e:
                        raise AsanaAPIError(
                            "Asana API response is not valid JSON",
                            status_code=None,
                            body=raw,
                        ) from e
                    if not isinstance(payload, dict):
                        raise AsanaAPIError(
                            "Asana API response is not a JSON object",
                            status_code=None,
                            body=raw,
                        )
                    return payload
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="replace")
                if e.code in _RETRY_STATUSES and attempt < _MAX_RETRIES:
                    attempt += 1
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60.0)
                    continue
                if e.code == 429:
                    raise AsanaRateLimitError(
                        "Asana rate limit (429) was not cleared after retries",
                        status_code=429,
                        body=err_body,
                    ) from e
                raise AsanaAPIError(
                    f"Asana API error: HTTP {e.code}",
                    status_code=e.code,
                    body=err_body,
                ) from e
            except (TimeoutError, urllib.error.URLError) as e:
                if is_get and _is_timeout(e) and timeout_attempt < _MAX_TIMEOUT_RETRIES:
                    timeout_attempt += 1
                    time.sleep(timeout_backoff)
                    timeout_backoff = min(timeout_backoff * 2, 60.0)
                    continue
                if is_get and _is_timeout(e):
                    raise AsanaAPIError("Asana API read timed out", status_code=None, body=None) from e
                if _is_timeout(e):
                    raise AsanaAPIError("Asana API write timed out", status_code=None, body=None) from e
                raise
