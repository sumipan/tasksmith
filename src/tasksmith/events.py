"""Tasksmith action events: types and safe emit."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol

__all__ = [
    "EVENT_KINDS",
    "EventKind",
    "EventSink",
    "NullSink",
    "TasksmithEvent",
    "first_line",
    "safe_emit",
    "utc_now_iso",
]

_log = logging.getLogger(__name__)

EventKind = Literal[
    "node_done",
    "node_blocked",
    "node_review",
    "human_requested",
    "review_requested",
    "human_reply",
    "elaborated",
    "template_created",
]

EVENT_KINDS: tuple[str, ...] = (
    "node_done",
    "node_blocked",
    "node_review",
    "human_requested",
    "review_requested",
    "human_reply",
    "elaborated",
    "template_created",
)


@dataclass(frozen=True)
class TasksmithEvent:
    kind: EventKind
    task_gid: str
    task_name: str  # "" when unavailable
    permalink_url: str  # "" when unavailable
    summary: str  # facts in 1-3 lines (second half of the per-kind template)
    execution_id: str  # execution_id created once per call
    actor: str  # same value as the actor passed to transition_task
    ts: str  # ISO8601 (UTC)


class EventSink(Protocol):
    def emit(self, event: TasksmithEvent) -> None: ...


class NullSink:
    """A sink that does nothing."""

    def emit(self, event: TasksmithEvent) -> None:
        return None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def first_line(text: str | None) -> str:
    """First non-empty line (``""`` if none)."""
    for line in (text or "").splitlines():
        s = line.strip()
        if s:
            return s
    return ""


def safe_emit(sink: EventSink | None, event: TasksmithEvent) -> None:
    """Do nothing when sink is None. Log emit exceptions with logging.warning and swallow them."""
    if sink is None:
        return
    try:
        sink.emit(event)
    except Exception as exc:  # noqa: BLE001 - an emit failure must not stop the main flow
        _log.warning(
            "[tasksmith] event emit failed kind=%s gid=%s: %s: %s",
            event.kind,
            event.task_gid,
            type(exc).__name__,
            exc,
        )
