"""Core vocabulary: owners, pipeline states, violations, and flow identifiers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = [
    "FLOW_DAG",
    "FLOW_PERSONAL",
    "OwnerType",
    "PipelineState",
    "Violation",
]


class OwnerType(str, Enum):
    AI = "AI"
    HUMAN = "HUMAN"
    EXTERNAL = "EXTERNAL"


class PipelineState(str, Enum):
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING_HUMAN = "WAITING_HUMAN"
    REVIEW = "REVIEW"
    DONE = "DONE"
    BLOCKED = "BLOCKED"
    # Completion marker of a normalize step; not a state-machine state.
    NORMALIZED = "NORMALIZED"


@dataclass(frozen=True)
class Violation:
    rule_id: str
    message: str
    auto_fixable: bool = False


FLOW_DAG = "dag"
FLOW_PERSONAL = "personal"
