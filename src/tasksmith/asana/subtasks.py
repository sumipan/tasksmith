"""Subtask ordering plans and setParent moves."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from tasksmith.asana.errors import AsanaAPIError
from tasksmith.transition import validate_subtask_row


@dataclass(frozen=True)
class SubtaskMove:
    """One setParent call."""

    gid: str
    insert_before: str | None
    insert_after: str | None


def plan_order(rows: Sequence[dict[str, Any]]) -> tuple[str, ...]:
    """Return target GID order: open with due, open without due, then completed."""
    seen_gids: set[str] = set()
    parsed: list[tuple[str, bool, bool, str, int]] = []

    for idx, row in enumerate(rows):
        gid, completed, due_str = validate_subtask_row(row, idx)
        if gid in seen_gids:
            raise ValueError(f"duplicate gid: {gid!r}")
        seen_gids.add(gid)
        no_due = not due_str
        parsed.append((gid, completed, no_due, due_str, idx))

    sorted_rows = sorted(parsed, key=lambda t: (t[1], t[2], t[3], t[4]))
    return tuple(t[0] for t in sorted_rows)


def plan_moves(current: Sequence[str], target: Sequence[str]) -> tuple[SubtaskMove, ...]:
    """Minimal moves from current to target order."""
    if len(current) != len(target):
        raise ValueError("current and target length mismatch")
    if set(current) != set(target):
        raise ValueError("current and target gid sets differ")
    if len(current) != len(set(current)):
        raise ValueError("duplicate gid in current")

    moves: list[SubtaskMove] = []
    work = list(current)

    for i, wanted in enumerate(target):
        if work[i] == wanted:
            continue
        j = work.index(wanted, i + 1)
        work.pop(j)
        work.insert(i, wanted)

        if i == 0:
            anchor_gid = work[1] if len(work) > 1 else None
            if anchor_gid is None:
                continue
            moves.append(SubtaskMove(gid=wanted, insert_before=anchor_gid, insert_after=None))
        else:
            prev_gid = work[i - 1]
            moves.append(SubtaskMove(gid=wanted, insert_before=None, insert_after=prev_gid))

    return tuple(moves)


def apply_order(
    parent_gid: str,
    moves: Sequence[SubtaskMove],
    *,
    client: Any,
) -> None:
    """Apply planned setParent moves via the REST client."""
    for move in moves:
        body: dict[str, Any] = {"parent": parent_gid}
        if move.insert_before is not None:
            body["insert_before"] = move.insert_before
        else:
            body["insert_after"] = move.insert_after
        client.request_json("POST", f"/tasks/{move.gid}/setParent", body=body)


def list_subtask_gids(rows: Sequence[dict[str, Any]]) -> tuple[str, ...]:
    """Validate list response rows and return GID tuple in API order."""
    current: list[str] = []
    for i, row in enumerate(rows):
        gid = str(row.get("gid") or "").strip()
        if not gid:
            raise AsanaAPIError(f"subtask row {i} has empty gid")
        current.append(gid)
    return tuple(current)
