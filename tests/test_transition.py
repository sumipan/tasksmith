"""State transitions. See issue #7527 AC2–AC3."""

from __future__ import annotations

import pytest

from tasksmith import LocalTaskStore
from tasksmith.body import build_controlled_notes, parse_sections, serialize_sections
from tasksmith.execution import set_executor
from tasksmith.model import OwnerType, PipelineState
from tasksmith.runtime import set_task_store
from tasksmith.settings import Settings, set_settings
from tasksmith.transition import transition_task


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


def test_transition_running_and_done_round_trip(orchestration_env) -> None:
    """Refs: #7527 AC2."""
    store = orchestration_env
    body = build_controlled_notes(
        objective="Ship it",
        owner=OwnerType.HUMAN,
        state=PipelineState.READY,
        flow="dag",
        acceptance=["The report is saved at docs/report.md"],
    )
    gid = store.create_task("Ship", body=body)
    assert (
        transition_task(gid, state=PipelineState.RUNNING, actor="test", execution_id="e1")
        == PipelineState.RUNNING
    )
    task = store.get_task(gid)
    secs = parse_sections(task["body"])
    assert secs and "state: RUNNING" in secs["control"]
    stories = store.get_task_stories(gid)
    assert any("PIPELINE_STATUS: RUNNING" in s.get("text", "") for s in stories)

    transition_task(gid, state=PipelineState.DONE, actor="test", execution_id="e2")
    assert store.get_task(gid)["completed"] is True


def test_transition_ready_stays_review_when_gate_fails(orchestration_env) -> None:
    """Refs: #7527 AC3."""
    store = orchestration_env
    body = build_controlled_notes(
        objective="Ship it",
        owner=OwnerType.HUMAN,
        state=PipelineState.REVIEW,
        flow="dag",
        acceptance=["The report is saved at docs/report.md"],
    )
    sections = parse_sections(body)
    assert sections is not None
    sections["acceptance"] = ""
    body = serialize_sections(sections)
    gid = store.create_task("Gate", body=body)

    got = transition_task(gid, state=PipelineState.READY, actor="test", execution_id="e1")
    assert got == PipelineState.REVIEW
    secs = parse_sections(store.get_task(gid)["body"])
    assert secs and "state: REVIEW" in secs["control"]
