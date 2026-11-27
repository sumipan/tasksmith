"""Subtask ordering plans."""

from __future__ import annotations

from tasksmith.asana.subtasks import SubtaskMove, plan_moves, plan_order


def _row(gid: str, completed: bool = False, due_on: str | None = None) -> dict:
    return {"gid": gid, "completed": completed, "due_on": due_on}


def test_subtask_order_and_moves_are_stable() -> None:
    """Issue #8119: due-date ordering and minimal move plan."""
    rows = [
        _row("d", completed=True),
        _row("b", due_on="2026-09-20"),
        _row("c"),
        _row("a", due_on="2026-09-18"),
        _row("e", completed=True, due_on="2026-01-01"),
    ]
    target = plan_order(rows)
    assert target == ("a", "b", "c", "d", "e")

    moves = plan_moves(("a", "b", "c"), ("b", "c", "a"))
    assert moves == (SubtaskMove(gid="a", insert_before=None, insert_after="c"),)
