"""Asana-specific exceptions layered on task store errors."""

from __future__ import annotations

from tasksmith.store import TaskStoreError


class AsanaError(TaskStoreError):
    """Base error for the Asana integration."""


class AsanaConfigError(AsanaError):
    """Invalid client configuration."""


class AsanaAPIError(AsanaError):
    """HTTP or malformed API response."""

    def __init__(self, message: str, status_code: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class AsanaRateLimitError(AsanaAPIError):
    """429 Too Many Requests after retries are exhausted."""
