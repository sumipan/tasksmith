"""Task storage port (protocol) and shared exceptions."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class TaskStoreError(Exception):
    """Base error for task store operations."""


class TaskNotFoundError(TaskStoreError):
    """Raised when a task identifier does not exist."""


@runtime_checkable
class TaskStore(Protocol):
    """Port for task records and history.

    Task dictionaries returned by ``get_task`` and ``list_tasks`` use:

    - ``gid`` (``str``)
    - ``name`` (``str``)
    - ``completed`` (``bool``)
    - ``due_on`` (``str | None``)
    - ``body`` (``str``; empty string when unset). Plain text whose content the
      protocol does not interpret; callers write Markdown. Any conversion to a
      storage-specific format (such as HTML) happens inside each implementation.
    - ``permalink_url`` (``str``)
    - ``parent`` (``{"gid": str} | None``)
    - ``section_gid`` (``str | None``)
    - ``completed_at`` (``str | None``): ISO 8601 UTC when completed; ``None`` if incomplete
    - ``subtasks`` (``list[dict]``): direct children only, sorted by numeric ``gid``.
      Each child uses the same keys except ``subtasks`` is omitted on children.

    Story dictionaries use ``gid``, ``type`` (``comment`` or ``system``), ``text``,
    ``created_at``, and ``created_by`` (``{"name": ...}``).
    """

    def get_task(self, task_gid: str) -> dict:
        """Return one task by id."""

    def list_tasks(self, project_gid: str | None = None) -> list[dict]:
        """Return top-level tasks for a project (or all projects when omitted).

        Includes completed tasks; each row exposes ``completed_at`` when completed.
        """

    def get_task_stories(self, task_gid: str) -> list[dict]:
        """Return comment and system stories in append order."""

    def create_task(
        self,
        name: str,
        *,
        project_gid: str | None = None,
        due_on: str | None = None,
        section_gid: str | None = None,
        body: str | None = None,
        assign_owner: str | None = None,
    ) -> str:
        """Create a top-level task and return its id."""

    def create_subtask(
        self,
        parent_gid: str,
        name: str,
        *,
        project_gid: str | None = None,
        due_on: str | None = None,
        section_gid: str | None = None,
        body: str | None = None,
        assign_owner: str | None = None,
    ) -> str:
        """Create a child task and return its id."""

    def update_task(
        self,
        task_gid: str,
        *,
        name: str | None = None,
        due_on: str | None = None,
        section_gid: str | None = None,
        body: str | None = None,
        completed: bool | None = None,
    ) -> None:
        """Patch fields that are not ``None``."""

    def add_comment(self, task_gid: str, text: str) -> None:
        """Append a comment story."""

    def ai_actor_names(self) -> frozenset[str]:
        """Names treated as automation when reading story authors."""
