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
    parsed: list[tuple[str, bool, str, int]] = []

    for idx, row in enumerate(rows):
        gid, completed, due_str = validate_subtask_row(row, idx)
        if gid in seen_gids:
            raise ValueError(f"duplicate gid: {gid!r}")
        seen_gids.add(gid)
        parsed.append((gid, completed, due_str, idx))

    def order_key(row: tuple[str, bool, str, int]) -> tuple[int, str, int]:
        _gid, completed, due_str, original_index = row
        if completed:
            return (2, "", original_index)
        if due_str:
            return (0, due_str, original_index)
        return (1, "", original_index)

    sorted_rows = sorted(parsed, key=order_key)
    return tuple(t[0] for t in sorted_rows)


def plan_moves(current: Sequence[str], target: Sequence[str]) -> tuple[SubtaskMove, ...]:
    """Minimal moves from current to target order."""
    if len(current) != len(target):
        raise ValueError("current and target length mismatch")
    if set(current) != set(target):
        raise ValueError("current and target gid sets differ")
    if len(current) != len(set(current)):
        raise ValueError("duplicate gid in current")

    # Keep one longest common subsequence in place. Every other item must move at
    # least once, so emitting exactly one move per non-LCS item is minimal.
    n = len(current)
    lcs_lengths = [[0] * (n + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(n - 1, -1, -1):
            if current[i] == target[j]:
                lcs_lengths[i][j] = 1 + lcs_lengths[i + 1][j + 1]
            else:
                lcs_lengths[i][j] = max(lcs_lengths[i + 1][j], lcs_lengths[i][j + 1])

    kept: set[str] = set()
    i = j = 0
    while i < n and j < n:
        if current[i] == target[j]:
            kept.add(current[i])
            i += 1
            j += 1
        elif lcs_lengths[i + 1][j] >= lcs_lengths[i][j + 1]:
            i += 1
        else:
            j += 1

    moves: list[SubtaskMove] = []
    work = list(current)

    def move_before(wanted: str, anchor: str) -> None:
        work.remove(wanted)
        work.insert(work.index(anchor), wanted)
        moves.append(SubtaskMove(gid=wanted, insert_before=anchor, insert_after=None))

    def move_after(wanted: str, anchor: str) -> None:
        work.remove(wanted)
        work.insert(work.index(anchor) + 1, wanted)
        moves.append(SubtaskMove(gid=wanted, insert_before=None, insert_after=anchor))

    # Fill each target gap around the kept subsequence. Items before the first
    # anchor are inserted right-to-left; all later gaps are inserted left-to-right.
    cursor = 0
    previous_anchor: str | None = None
    for target_index, kept_gid in enumerate(target):
        if kept_gid not in kept:
            continue
        gap = target[cursor:target_index]
        if previous_anchor is None:
            anchor = kept_gid
            for wanted in reversed(gap):
                move_before(wanted, anchor)
                anchor = wanted
        else:
            anchor = previous_anchor
            for wanted in gap:
                move_after(wanted, anchor)
                anchor = wanted
        previous_anchor = kept_gid
        cursor = target_index + 1

    if previous_anchor is not None:
        anchor = previous_anchor
        for wanted in target[cursor:]:
            move_after(wanted, anchor)
            anchor = wanted

    if tuple(work) != tuple(target):
        raise AssertionError("move plan did not reach target order")
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
