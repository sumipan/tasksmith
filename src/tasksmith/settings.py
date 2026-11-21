"""Runtime settings the tasksmith core receives from the host (value injection)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tasksmith.sections import SectionsConfig

__all__ = [
    "Settings",
    "set_settings",
    "settings",
    "settings_installed",
]

_settings: Settings | None = None


@dataclass(frozen=True)
class Settings:
    state_dir: Path
    project_id: str | None
    executor_exists: Callable[[str], bool]
    request_channel_id: str | None = None
    timezone: str = "UTC"
    sections: SectionsConfig | None = None
    remind_after_hours: int = 24
    remind_max_count: int = 3
    review_daily_cap: int = 3
    quiet_hours: tuple[str, str] | None = None
    digest_at: str | None = None
    probes: Mapping[str, Any] = field(default_factory=dict)


def settings() -> Settings:
    if _settings is None:
        raise RuntimeError(
            "tasksmith Settings is not set; call tasksmith.settings.set_settings(...) first."
        )
    return _settings


def set_settings(value: Settings | None) -> None:
    global _settings
    _settings = value


def settings_installed() -> bool:
    return _settings is not None
