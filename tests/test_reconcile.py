"""Reconcile classification. See issue #7527 AC4–AC5."""

from __future__ import annotations

import pytest

from tasksmith import LocalTaskStore
from tasksmith.body import build_controlled_notes, update_meta_field
from tasksmith.execution import set_executor
from tasksmith.model import OwnerType, PipelineState
from tasksmith.reconcile import reconcile
from tasksmith.runtime import set_task_store
from tasksmith.settings import Settings, set_settings
from tasksmith.transition import transition_task


def _ready_human_body(objective: str = "Work", *, depends: str | None = None) -> str:
    body = build_controlled_notes(
        objective=objective,
        owner=OwnerType.HUMAN,
        state=PipelineState.READY,
        flow="dag",
        acceptance=["Deliver docs/report.md"],
    )
    if depends:
        body = update_meta_field(body, "depends_on", depends, create=True)
    return body


@pytest.fixture
def orchestration_env(tmp_path, monkeypatch):
    """Install a local store and settings; clear overrides after the test."""
    store_root = tmp_path / "store"
    state_dir = tmp_path / "state"
    store = LocalTaskStore(store_root)
    set_task_store(store)
    set_settings(
        Settings(
            state_dir=state_dir,
            project_id=None,
            executor_exists=lambda name: name == "demo",
        )
    )
    monkeypatch.delenv("TASKSMITH_ROOT", raising=False)
    yield store
    set_task_store(None)
    set_settings(None)
    set_executor(None)


def test_reconcile_respects_depends_on_chain(orchestration_env) -> None:
    """Refs: #7527 AC4."""
    store = orchestration_env
    gid_a = store.create_task("A", body=_ready_human_body("A"))
    gid_b = store.create_task(
        "B",
        body=_ready_human_body("B", depends=gid_a),
    )

    r1 = reconcile()
    assert [n.task_gid for n in r1.ready] == [gid_a]
    assert any(n.task_gid == gid_b for n in r1.blocked)

    transition_task(gid_a, state=PipelineState.DONE, actor="test", execution_id="e1")
    r2 = reconcile()
    assert [n.task_gid for n in r2.ready] == [gid_b]


def test_reconcile_detects_dependency_cycle(orchestration_env) -> None:
    """Refs: #7527 AC5."""
    store = orchestration_env
    gid_a = store.create_task("A", body=_ready_human_body("A"))
    gid_b = store.create_task("B", body=_ready_human_body("B"))
    store.update_task(
        gid_a,
        body=update_meta_field(store.get_task(gid_a)["body"], "depends_on", gid_b, create=True),
    )
    store.update_task(
        gid_b,
        body=update_meta_field(store.get_task(gid_b)["body"], "depends_on", gid_a, create=True),
    )

    r = reconcile()
    assert r.ready == []
    blocked = {n.task_gid for n in r.blocked}
    assert gid_a in blocked and gid_b in blocked
