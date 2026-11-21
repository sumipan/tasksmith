"""Ghdag executor. See issue #7527 AC8."""

from __future__ import annotations

import json

import pytest

from tasksmith.execution import GhdagExecutor


@pytest.mark.parametrize("route", [(("claude", "sonnet"),)])
def test_ghdag_executor_enqueue_idempotent(tmp_path, route) -> None:
    """Refs: #7527 AC8."""
    queue = tmp_path / "queue"
    queue.mkdir()
    state = tmp_path / "state"
    ex = GhdagExecutor(
        queue_dir=queue,
        state_dir=state,
        route=list(route),
        finish_template="echo done",
    )
    uid = ex.submit("t1", "prompt", idempotency_key="k1", meta_digest="d1")
    assert isinstance(uid, str)
    lines = (queue / "exec.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert second.get("depends") == [first["uuid"]]

    assert ex.submit("t1", "prompt", idempotency_key="k1", meta_digest="d1") is None
    assert len((queue / "exec.jsonl").read_text(encoding="utf-8").strip().splitlines()) == 2
