"""Pytest fixtures for tasksmith tests."""

from __future__ import annotations

import pytest

from tasksmith import LocalTaskStore
from tasksmith.execution import set_executor
from tasksmith.runtime import set_task_store
from tasksmith.settings import Settings, set_settings


@pytest.fixture
def orchestration_env(tmp_path, monkeypatch):
    """Install a local store and settings; clear overrides after the test."""
    store_root = tmp_path / "store"
    state_dir = tmp_path / "state"
    store = LocalTaskStore(store_root)
    set_task_store(store)
    set_settings(
        Settings(
            state_dir=state_dir,
            project_id=None,
            executor_exists=lambda name: name == "demo",
        )
    )
    monkeypatch.delenv("TASKSMITH_ROOT", raising=False)
    yield store
    set_task_store(None)
    set_settings(None)
    set_executor(None)
