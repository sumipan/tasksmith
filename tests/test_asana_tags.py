"""Tag sync from controlled bodies."""

from __future__ import annotations

from unittest.mock import MagicMock

from tasksmith.asana import tags
from tasksmith.body import build_controlled_notes
from tasksmith.tags import set_tags


def test_tag_sync_applies_only_the_diff() -> None:
    """Issue #8119: only missing index and user tags are added or removed."""
    body = build_controlled_notes(
        objective="x",
        state="READY",
        owner="AI",
    )
    body = set_tags(body, ["foo", "bar"])
    client = MagicMock()
    cache: dict[str, str] = {
        name: f"gid-{name}" for name in tags._INDEX_NAMES
    }
    def index_json(method, path, *, query=None, body=None):
        del query
        if method == "GET":
            return {
                "data": {
                    "gid": "t1",
                    "tags": [
                        {"gid": "tg1", "name": "state:DONE"},
                        {"gid": "tg2", "name": "owner:HUMAN"},
                        {"gid": "tg3", "name": "legacy"},
                    ],
                }
            }
        return {}

    client.request_json.side_effect = index_json
    actions = tags.sync_index_tags(
        "t1", body, workspace_gid="ws", client=client, index_cache=cache
    )
    assert "removed state:DONE" in actions
    assert "added state:READY" in actions

    def user_json(method, path, *, query=None, body=None):
        del query
        if method == "GET" and path == "/tasks/t1":
            return {"data": {"gid": "t1", "tags": [{"gid": "u1", "name": "legacy"}]}}
        if method == "GET":
            return {"data": []}
        if method == "POST" and path == "/tags":
            name = body["name"] if isinstance(body, dict) else "t"
            return {"data": {"gid": f"new-{name}", "name": name}}
        return {}

    client.request_json.side_effect = user_json
    user_cache: dict[str, dict[str, str]] = {}
    user_actions = tags.sync_user_tags(
        "t1", body, workspace_gid="ws", client=client, user_cache=user_cache
    )
    assert any(a.startswith("added foo") for a in user_actions)


def test_missing_tags_line_preserves_user_tags() -> None:
    """Issue #8119: no tags: line means user tags are left unchanged."""
    body = build_controlled_notes(objective="x", state="READY", owner="AI")
    client = MagicMock()
    actions = tags.sync_user_tags(
        "t1", body, workspace_gid="ws", client=client, user_cache={}
    )
    assert actions == []
    client.request_json.assert_not_called()
