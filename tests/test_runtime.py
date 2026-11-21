"""Runtime task store wiring. See issue #7527 AC1."""

from __future__ import annotations

import pytest

from tasksmith import LocalTaskStore
from tasksmith.runtime import set_task_store, task_store


def test_task_store_requires_override_or_env(tmp_path, monkeypatch):
    """Refs: #7527 AC1."""
    set_task_store(None)
    monkeypatch.delenv("TASKSMITH_ROOT", raising=False)
    with pytest.raises(RuntimeError, match="TASKSMITH_ROOT"):
        task_store()

    monkeypatch.setenv("TASKSMITH_ROOT", str(tmp_path))
    got = task_store()
    assert isinstance(got, LocalTaskStore)

    custom = LocalTaskStore(tmp_path / "other")
    set_task_store(custom)
    assert task_store() is custom
    set_task_store(None)
