"""Task storage port (protocol) and shared exceptions."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


class TaskStoreError(Exception):
    """Base error for task store operations."""


class TaskNotFoundError(TaskStoreError):
    """Raised when a task identifier does not exist."""


@runtime_checkable
class TaskStore(Protocol):
    """Port for task records, history, and tags.

    Task dictionaries use keys such as ``gid``, ``name``, ``completed``, ``due_on``,
    ``notes``, ``html_notes``, ``permalink_url``, ``tags`` (``[{"gid", "name"}]``),
    and ``parent`` (``{"gid"}`` or ``None``).

    Story dictionaries use ``gid``, ``type`` (``comment`` or ``system``), ``text``,
    ``created_at``, and ``created_by`` (``{"name": ...}``).

    Tag dictionaries use ``gid`` and ``name``.
    """

    def get_task(
        self,
        task_gid: str,
        *,
        opt_fields: str | None = None,
        client: Any = None,
    ) -> dict:
        """Return one task by id."""

    def list_tasks(
        self,
        project_gid: str | None = None,
        *,
        opt_fields: str | None = None,
        client: Any = None,
    ) -> list[dict]:
        """Return top-level tasks for a project (or all projects when omitted)."""

    def get_task_stories(
        self,
        task_gid: str,
        *,
        client: Any = None,
    ) -> list[dict]:
        """Return comment and system stories in append order."""

    def create_task(
        self,
        name: str,
        *,
        project_gid: str | None = None,
        due_on: str | None = None,
        section_gid: str | None = None,
        notes: str | None = None,
        html_notes: str | None = None,
        assign_owner: str | None = None,
        client: Any = None,
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
        notes: str | None = None,
        html_notes: str | None = None,
        assign_owner: str | None = None,
        client: Any = None,
    ) -> str:
        """Create a child task and return its id."""

    def update_task(
        self,
        task_gid: str,
        *,
        name: str | None = None,
        due_on: str | None = None,
        notes: str | None = None,
        html_notes: str | None = None,
        completed: bool | None = None,
        assignee: str | None = None,
        custom_fields: dict | None = None,
        client: Any = None,
    ) -> None:
        """Patch fields that are not ``None``."""

    def add_comment(
        self,
        task_gid: str,
        text: str,
        *,
        client: Any = None,
    ) -> None:
        """Append a comment story."""

    def list_workspace_tags(
        self,
        workspace_gid: str,
        *,
        client: Any = None,
    ) -> list[dict]:
        """List tags in the workspace (backend-defined scope)."""

    def create_tag(
        self,
        workspace_gid: str,
        *,
        name: str,
        client: Any = None,
    ) -> dict:
        """Create a tag and return its record."""

    def add_tag_to_task(
        self,
        task_gid: str,
        *,
        tag_gid: str,
        client: Any = None,
    ) -> None:
        """Attach a tag to a task if not already present."""

    def remove_tag_from_task(
        self,
        task_gid: str,
        *,
        tag_gid: str,
        client: Any = None,
    ) -> None:
        """Detach a tag from a task when present."""

    def ai_actor_names(self, client: Any = None) -> frozenset[str]:
        """Names treated as automation when reading story authors."""

    def default_client(self) -> Any:
        """Return the default connection object for this implementation."""
