"""Task orchestration storage layer."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from tasksmith.local import LocalTaskStore
from tasksmith.store import TaskNotFoundError, TaskStore, TaskStoreError

try:
    __version__ = version("tasksmith")
except PackageNotFoundError:
    __version__ = "0+unknown"

__all__ = [
    "LocalTaskStore",
    "TaskNotFoundError",
    "TaskStore",
    "TaskStoreError",
    "__version__",
]
