"""Contract tests for TaskStore implementations. See Issue #7328."""

from __future__ import annotations

from unittest.mock import MagicMock

from tasksmith import LocalTaskStore, TaskStore
from tasksmith.asana import AsanaTaskStore


def test_local_store_satisfies_the_protocol(tmp_path) -> None:
    """AC7: LocalTaskStore is a runtime TaskStore."""
    store = LocalTaskStore(tmp_path)
    assert isinstance(store, TaskStore)


def test_asana_store_satisfies_runtime_protocol() -> None:
    """Issue #8119: AsanaTaskStore satisfies the TaskStore protocol."""
    fake = MagicMock()

    def factory(_token: str, _role: str) -> MagicMock:
        return fake

    store = AsanaTaskStore(
        writer_token="w",
        project_gid="p",
        client_factory=factory,
    )
    assert isinstance(store, TaskStore)
