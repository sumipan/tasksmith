"""Asana REST helper functions."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tasksmith.asana import api
from tasksmith.asana.errors import AsanaAPIError


def test_api_search_falls_back_on_payment_required() -> None:
    """Issue #8119: HTTP 402 search falls back to project list filtering."""
    client = MagicMock()

    def request_json(method, path, *, query=None, body=None):
        del body
        if path.endswith("/tasks/search"):
            raise AsanaAPIError("payment required", status_code=402)
        if path.endswith("/tasks"):
            return {
                "data": [
                    {"gid": "1", "name": "Alpha task", "completed": False},
                    {"gid": "2", "name": "beta", "completed": False},
                    {"gid": "3", "name": "Alpha done", "completed": True},
                ],
                "next_page": None,
            }
        raise AssertionError((method, path))

    client.request_json.side_effect = request_json
    out = api.search_tasks(
        "alpha",
        workspace_gid="ws",
        project_gid="proj",
        limit=10,
        client=client,
    )
    assert [r["gid"] for r in out] == ["1"]


def test_api_rejects_malformed_response_envelopes() -> None:
    """Issue #8119: missing data or gid raises AsanaAPIError."""
    client = MagicMock()
    client.request_json.return_value = {"data": "not-a-dict"}
    with pytest.raises(AsanaAPIError):
        api.get_task("1", opt_fields="name", client=client)

    client.request_json.return_value = {"data": {"name": "x"}}
    with pytest.raises(AsanaAPIError):
        api.create_task("n", project_gid="p", due_on=None, notes=None, html_notes=None, client=client)
