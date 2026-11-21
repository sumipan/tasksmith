"""Task state transitions and parent due-date rollup."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tasksmith.body import (
    is_controlled_notes,
    parse_meta_field,
    parse_sections,
    update_meta_field,
)
from tasksmith.gates import check_design_doc, format_violations_comment
from tasksmith.language import language
from tasksmith.model import PipelineState
from tasksmith.runtime import task_store

__all__ = [
    "RollupResult",
    "pipeline_status_comment",
    "plan_parent_due",
    "rollup_parent_due",
    "rollup_parent_of_task",
    "transition_task",
    "validate_subtask_row",
]

_log = logging.getLogger(__name__)

def pipeline_status_comment(
    *, state: PipelineState, actor: str, execution_id: str, detail: str = ""
) -> str:
    """Audit marker at the end of a story, readable by both machines and people."""
    body = detail.strip()
    if body:
        body += "\n\n"
    return f"{body}PIPELINE_STATUS: {state.value}\nACTOR: {actor}\nEXECUTION_ID: {execution_id}"

def transition_task(
    task_gid: str,
    *,
    state: PipelineState,
    actor: str,
    execution_id: str,
    detail: str = "",
) -> PipelineState:
    """Update the body state: line and the audit comment, in this order (index tags are synced by the store implementation).

    When ``state=READY`` and ``check_design_doc`` reports violations, the task goes to
    REVIEW instead of READY. Return the state actually written.
    With ``state=DONE``, ``completed=True`` is set.
    On a mid-way failure the rest is skipped and an exception is raised (picked up as
    drift by reconcile).
    """
    target_state = state
    current = task_store().get_task(
        task_gid,
    )
    body = current.get("body")
    if not isinstance(body, str):
        raise ValueError("Task has no body.")
    sections = parse_sections(body)
    if sections is None or not is_controlled_notes(body):
        raise ValueError("Task body is not in the fixed Control Plane format.")
    old_state = parse_meta_field(sections, "state")
    if not old_state:
        raise ValueError("Control metadata has no state: line.")

    gate_detail = detail
    if target_state == PipelineState.READY:
        violations = check_design_doc(sections, body=body)
        if violations:
            target_state = PipelineState.REVIEW
            listing = format_violations_comment(violations)
            gate_text = language().messages["control_plane.gate_violation"].format(
                listing=listing
            )
            gate_detail = (f"{detail}\n\n{gate_text}" if detail else gate_text).strip()

    updated_body = update_meta_field(body, "state", target_state.value)
    task_store().update_task(task_gid, body=updated_body)

    task_store().add_comment(
        task_gid,
        pipeline_status_comment(
            state=target_state, actor=actor, execution_id=execution_id, detail=gate_detail
        ),
    )

    if target_state == PipelineState.DONE:
        task_store().update_task(task_gid, completed=True)
        try:
            rollup_parent_of_task(task_gid)
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "[asana] rollup_parent_of_task failed after DONE transition task=%s",
                task_gid, exc_info=True,
            )
    return target_state


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate_subtask_row(row: Mapping[str, Any], idx: int) -> tuple[str, bool, str]:
    """Row check shared by the subtask reorder plan and parent due-date rollup. Return (gid, completed, due_str)."""
    gid = row.get("gid")
    if not isinstance(gid, str) or not gid:
        raise ValueError(f"Row {idx} gid is empty or not a string: {gid!r}")

    completed = row.get("completed")
    if not isinstance(completed, bool):
        raise ValueError(f"gid={gid!r} completed is not a bool: {completed!r}")

    due_on = row.get("due_on")
    if due_on is not None and due_on != "" and not isinstance(due_on, str):
        raise ValueError(f"gid={gid!r} due_on is invalid: {due_on!r}")
    if isinstance(due_on, str) and due_on and not _DATE_RE.match(due_on):
        raise ValueError(f"gid={gid!r} due_on is not in YYYY-MM-DD format: {due_on!r}")

    due_str = due_on if isinstance(due_on, str) and due_on else ""
    return gid, completed, due_str


def plan_parent_due(rows: Sequence[Mapping[str, Any]]) -> str | None:
    """Pure function deciding the parent due date from the direct subtasks' due_on / completed."""
    min_due: str | None = None
    for idx, row in enumerate(rows):
        _gid, completed, due_str = validate_subtask_row(row, idx)
        if not completed and due_str:
            if min_due is None or due_str < min_due:
                min_due = due_str
    return min_due


@dataclass(frozen=True)
class RollupResult:
    parent_gid: str
    before: str | None
    after: str | None
    changed: bool


def rollup_parent_due(parent_gid: str, *, dry_run: bool = False) -> RollupResult:
    """Aggregate the parent due_on from the direct subtasks' due dates."""
    parent = task_store().get_task(parent_gid)
    rows = parent.get("subtasks") or []
    before = parent.get("due_on")
    if before is not None and before != "" and not isinstance(before, str):
        before = None
    if before == "":
        before = None

    after = plan_parent_due(rows)
    changed = after is not None and after != before

    if changed and not dry_run:
        task_store().update_task(parent_gid, due_on=after)

    return RollupResult(
        parent_gid=parent_gid,
        before=before,
        after=after,
        changed=changed,
    )


def rollup_parent_of_task(task_gid: str, *, dry_run: bool = False) -> RollupResult | None:
    """Aggregate the due_on of the direct parent of task_gid. ``None`` when there is no parent."""
    task = task_store().get_task(task_gid)
    parent = task.get("parent")
    if not isinstance(parent, dict) or not parent.get("gid"):
        return None
    parent_gid = str(parent["gid"])
    return rollup_parent_due(parent_gid, dry_run=dry_run)
