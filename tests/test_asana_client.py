"""Asana HTTP client behavior."""

from __future__ import annotations

import io
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from tasksmith.asana.client import AsanaClient
from tasksmith.asana.errors import AsanaAPIError, AsanaConfigError, AsanaRateLimitError


def _ok_ctx(payload: bytes = b'{"data":{"gid":"1"}}') -> MagicMock:
    ctx = MagicMock()
    ctx.read.return_value = payload
    ctx.__enter__.return_value = ctx
    ctx.__exit__.return_value = None
    return ctx


def test_client_retries_transient_statuses_with_backoff() -> None:
    """Issue #8119: 429 and transient 5xx retry up to three times."""
    client = AsanaClient("token", role="writer")
    err429 = urllib.error.HTTPError("url", 429, "x", hdrs={}, fp=io.BytesIO(b"{}"))
    err503 = urllib.error.HTTPError("url", 503, "x", hdrs={}, fp=io.BytesIO(b"{}"))

    for code, err in ((429, err429), (503, err503)):
        side = [err, err, err, _ok_ctx()]
        with (
            patch("urllib.request.urlopen", side_effect=side) as m_open,
            patch("time.sleep"),
        ):
            out = client.request_json("GET", "/tasks/1")
        assert out["data"]["gid"] == "1"
        assert m_open.call_count == 4

    permanent = [err429, err429, err429, err429]
    with (
        patch("urllib.request.urlopen", side_effect=permanent),
        patch("time.sleep"),
        pytest.raises(AsanaRateLimitError) as ei,
    ):
        client.request_json("GET", "/tasks/1")
    assert ei.value.status_code == 429


def test_client_retries_only_get_timeouts() -> None:
    """Issue #8119: only GET read timeouts retry; POST timeouts and reader POST do not."""
    client = AsanaClient("token", role="writer")
    side = [TimeoutError("read timed out"), TimeoutError("read timed out"), _ok_ctx()]
    with (
        patch("urllib.request.urlopen", side_effect=side) as m_open,
        patch("time.sleep"),
    ):
        out = client.request_json("GET", "/tasks/1")
    assert out["data"]["gid"] == "1"
    assert m_open.call_count == 3

    reader = AsanaClient("token", role="reader")
    with pytest.raises(AsanaConfigError):
        reader.request_json("POST", "/tasks", body={"name": "n"})

    with (
        patch("urllib.request.urlopen", side_effect=TimeoutError("t")) as m_open,
        patch("time.sleep"),
        pytest.raises(AsanaAPIError),
    ):
        client.request_json("POST", "/tasks", body={"name": "n"})
    assert m_open.call_count == 1


@pytest.mark.parametrize("payload", [b"[]", b"not-json"])
def test_client_rejects_non_object_json(payload: bytes) -> None:
    """Issue #8119: malformed and non-object JSON responses use AsanaAPIError."""
    client = AsanaClient("token", role="reader")
    with (
        patch("urllib.request.urlopen", return_value=_ok_ctx(payload)),
        pytest.raises(AsanaAPIError),
    ):
        client.request_json("GET", "/tasks/1")
