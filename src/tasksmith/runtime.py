"""Runtime wiring for the task store port."""

from __future__ import annotations

import os
from pathlib import Path

from tasksmith.local import LocalTaskStore
from tasksmith.store import TaskStore

__all__ = ["set_task_store", "task_store"]

_override: TaskStore | None = None


def set_task_store(store: TaskStore | None) -> None:
    global _override
    _override = store


def task_store() -> TaskStore:
    if _override is not None:
        return _override
    root = os.environ.get("TASKSMITH_ROOT", "").strip()
    if root:
        return LocalTaskStore(Path(root))
    raise RuntimeError(
        "tasksmith TaskStore is not set; call tasksmith.runtime.set_task_store(...) or set TASKSMITH_ROOT"
    )
