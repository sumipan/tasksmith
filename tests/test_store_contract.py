"""Contract tests for TaskStore implementations. See Issue #7328."""

from __future__ import annotations

from tasksmith import LocalTaskStore, TaskStore


def test_local_store_satisfies_the_protocol(tmp_path) -> None:
  """AC7: LocalTaskStore is a runtime TaskStore."""
  store = LocalTaskStore(tmp_path)
  assert isinstance(store, TaskStore)
