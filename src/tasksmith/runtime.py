"""Runtime wiring for the task store port and human channel."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from tasksmith.local import LocalTaskStore
from tasksmith.store import TaskStore

if TYPE_CHECKING:
    from tasksmith.human import HumanChannel

__all__ = [
    "human_channel",
    "human_channel_installed",
    "set_human_channel",
    "set_task_store",
    "task_store",
]

_override: TaskStore | None = None
_human_channel_override: HumanChannel | None = None


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


def human_channel() -> HumanChannel:
    if _human_channel_override is not None:
        return _human_channel_override
    root = os.environ.get("TASKSMITH_ROOT", "").strip()
    if root:
        from tasksmith.file_channel import FileChannel

        return FileChannel(Path(root) / ".tasks")
    raise RuntimeError(
        "human channel is not installed; call set_human_channel() or set TASKSMITH_ROOT"
    )


def set_human_channel(channel: HumanChannel | None) -> None:
    global _human_channel_override
    _human_channel_override = channel


def human_channel_installed() -> bool:
    if _human_channel_override is not None:
        return True
    return bool(os.environ.get("TASKSMITH_ROOT", "").strip())
