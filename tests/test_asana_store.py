"""Asana TaskStore wiring."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

from tasksmith.asana.errors import AsanaAPIError
from tasksmith.asana.store import AsanaTaskStore
from tasksmith.body import build_controlled_notes
from tasksmith.store import TaskNotFoundError


def _store_with_clients(
    reader: Any, writer: Any, **kwargs: Any
) -> AsanaTaskStore:
    roles: dict[str, Any] = {"reader": reader, "writer": writer}

    def factory(token: str, role: str) -> Any:
        del token
        return roles[role]

    return AsanaTaskStore(
        writer_token="w",
        reader_token="r",
        project_gid="proj",
        workspace_gid="ws",
        client_factory=factory,
        **kwargs,
    )


def test_store_normalizes_task_and_story_shapes() -> None:
    """Issue #8119: tasks and stories match TaskStore keys and child order."""
    reader = MagicMock()
    writer = MagicMock()
    reader.request_json.side_effect = [
        {
            "data": {
                "gid": "10",
                "name": "parent",
                "completed": False,
                "memberships": [
                    {"project": {"gid": "proj"}, "section": {"gid": "s1"}},
                ],
                "subtasks": [
                    {"gid": "12", "name": "second", "completed": True},
                    {"gid": "9", "name": "first", "completed": False},
                ],
            }
        },
        {
            "data": [
                {
                    "gid": "c1",
                    "type": "comment",
                    "text": "hi",
                    "created_at": "t",
                    "created_by": {"name": "Sam"},
                }
            ]
        },
    ]
    store = _store_with_clients(reader, writer)
    task = store.get_task("10")
    assert task["section_gid"] == "s1"
    assert [t["gid"] for t in task["subtasks"]] == ["9", "12"]
    assert all("subtasks" not in t for t in task["subtasks"])

    stories = store.get_task_stories("10")
    assert stories[0]["created_by"] == {"name": "Sam"}


def test_store_routes_reads_and_writes_to_token_roles() -> None:
    """Issue #8119: GET uses reader client; POST uses writer client."""
    reader = MagicMock()
    writer = MagicMock()
    reader.request_json.return_value = {
        "data": {"gid": "1", "name": "n", "completed": False, "memberships": []}
    }
    writer.request_json.return_value = {"data": {"gid": "new"}}
    store = _store_with_clients(reader, writer)
    store.get_task("1")
    store.create_task("x")

    assert reader.request_json.called
    assert writer.request_json.called
    assert all(call.args[0] == "GET" for call in reader.request_json.call_args_list)
    assert writer.request_json.call_args.args[0] == "POST"

    store_same = AsanaTaskStore(
        writer_token="only",
        project_gid="proj",
        workspace_gid="ws",
        client_factory=lambda token, role: MagicMock(name=role),
    )
    assert store_same._reader_token == store_same._writer_token


def test_list_tasks_includes_completed_on_every_page() -> None:
    """Issue #8119: every list page includes completed_since."""
    reader = MagicMock()
    writer = MagicMock()

    def list_pages(method, path, *, query=None, body=None):
        del method, body
        assert "completed_since=1970-01-01T00%3A00%3A00Z" in (query or "")
        if "offset=page2" in (query or ""):
            return {
                "data": [{"gid": "2", "name": "done", "completed": True, "memberships": []}],
                "next_page": None,
            }
        return {
            "data": [{"gid": "1", "name": "open", "completed": False, "memberships": []}],
            "next_page": {"offset": "page2"},
        }

    reader.request_json.side_effect = list_pages
    store = _store_with_clients(reader, writer)
    rows = store.list_tasks()
    assert len(rows) == 2
    assert rows[1]["completed"] is True
    assert reader.request_json.call_count == 2


def test_store_maps_404_to_task_not_found() -> None:
    """Issue #8119: HTTP 404 becomes TaskNotFoundError."""
    reader = MagicMock()
    reader.request_json.side_effect = AsanaAPIError("missing", status_code=404)
    store = _store_with_clients(reader, MagicMock())
    with pytest.raises(TaskNotFoundError) as ei:
        store.get_task("missing")
    assert ei.value.args[0] == "missing"


def test_store_counts_all_requests() -> None:
    """Issue #8119: calls matches request_json invocations through the store."""
    client = MagicMock()
    client.request_json.return_value = {"data": [], "next_page": None}

    def factory(_token: str, _role: str) -> Any:
        return client

    store = AsanaTaskStore(
        writer_token="w",
        project_gid="proj",
        workspace_gid="ws",
        client_factory=factory,
    )
    store.list_tasks()
    store.get_task_stories("t1")
    store.add_comment("t1", "hi")
    assert store.calls == client.request_json.call_count == 3


def test_post_write_sync_failure_is_best_effort(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Issue #8119: tag sync failure still returns created gid."""
    writer = MagicMock()
    writer.request_json.side_effect = [
        {"data": {"gid": "created"}},
        {"data": {"gid": "t1", "tags": []}},
        {"data": []},
        AsanaAPIError("tag fail"),
    ]
    store = _store_with_clients(MagicMock(), writer)
    body = build_controlled_notes(objective="x", state="READY", owner="AI")
    with caplog.at_level(logging.WARNING):
        gid = store.create_task("name", body=body)
    assert gid == "created"
    task_posts = [
        c
        for c in writer.request_json.call_args_list
        if c.args[0] == "POST" and c.args[1] == "/tasks"
    ]
    assert len(task_posts) == 1
