"""Local human-then-AI loop. Refs: issue #7528 AC1."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tasksmith.body import build_controlled_notes, parse_meta_field, parse_sections, update_meta_field
from tasksmith.execution import set_executor, set_thinker
from tasksmith.file_channel import FileChannel
from tasksmith.human import record_human_reply
from tasksmith.model import OwnerType, PipelineState
from tasksmith.reconcile import reconcile
from tasksmith.runtime import set_human_channel, set_task_store
from tasksmith.settings import Settings, set_settings
from tasksmith.tick import finish_node, tick


@dataclass
class RecordingExecutor:
    submits: list[tuple[str, str, str]] = field(default_factory=list)

    def used(self, idempotency_key: str) -> bool:
        del idempotency_key
        return False

    def failure(self, idempotency_key: str) -> str | None:
        del idempotency_key
        return None

    def submit(
        self,
        task_id: str,
        prompt: str,
        *,
        idempotency_key: str,
        meta_digest: str,
    ) -> str | None:
        del prompt
        self.submits.append((task_id, idempotency_key, meta_digest))
        return "job-uuid"


@dataclass
class ApproveThinker:
    def ask(self, prompt: str, *, weight: str, timeout: float) -> str:
        del prompt, weight, timeout
        return '```json\n{"decision": "approve", "choice": "", "note": "", "due_on": ""}\n```'


@pytest.fixture
def loop_env(tmp_path, monkeypatch):
    from tasksmith import LocalTaskStore

    root = tmp_path / "workspace"
    tasks_dir = root / ".tasks"
    store = LocalTaskStore(root)
    set_task_store(store)
    set_human_channel(FileChannel(tasks_dir))
    set_settings(
        Settings(
            state_dir=tasks_dir / "state",
            project_id=None,
            executor_exists=lambda name: name == "demo",
            request_channel_id=str(tasks_dir),
        )
    )
    ex = RecordingExecutor()
    set_executor(ex)
    set_thinker(ApproveThinker())
    monkeypatch.delenv("TASKSMITH_ROOT", raising=False)
    yield root, tasks_dir, store, ex
    set_task_store(None)
    set_human_channel(None)
    set_settings(None)
    set_executor(None)
    set_thinker(None)


def test_local_one_loop_human_then_ai(loop_env, tmp_path: Path) -> None:
    """Refs: #7528 AC1."""
    _root, tasks_dir, store, ex = loop_env
    body_a = build_controlled_notes(
        objective="Approve downstream AI work",
        owner=OwnerType.HUMAN,
        state=PipelineState.READY,
        flow="dag",
        acceptance=["Human reply is recorded"],
    )
    gid_a = store.create_task("human gate", body=body_a)
    body_b = build_controlled_notes(
        objective="Run demo skill",
        owner=OwnerType.AI,
        state=PipelineState.READY,
        flow="dag",
        executor="skill:demo",
        deliverables="- output.txt",
        acceptance=["Result has PIPELINE_STATUS: DONE"],
    )
    body_b = update_meta_field(body_b, "depends_on", gid_a, create=True)
    gid_b = store.create_task("ai node", body=body_b)

    tick()
    task_a = store.get_task(gid_a)
    secs_a = parse_sections(task_a["body"])
    assert secs_a is not None
    assert parse_meta_field(secs_a, "state") == PipelineState.WAITING_HUMAN.value
    assert (tasks_dir / "requests" / "1.md").is_file()
    assert len(ex.submits) == 0

    record_human_reply(gid_a, "ok", ref="1")
    task_a = store.get_task(gid_a)
    secs_a = parse_sections(task_a["body"])
    assert parse_meta_field(secs_a, "state") == PipelineState.DONE.value

    tick()
    task_b = store.get_task(gid_b)
    secs_b = parse_sections(task_b["body"])
    assert parse_meta_field(secs_b, "state") == PipelineState.RUNNING.value
    assert len(ex.submits) == 1
    assert ex.submits[0][0] == gid_b

    result_path = tmp_path / "result.md"
    result_path.write_text("PIPELINE_STATUS: DONE\n", encoding="utf-8")
    finish_node(gid_b, result_path=result_path, meta_digest=ex.submits[0][2])
    rec = reconcile()
    assert rec.ready == []
    assert rec.waiting_human == []
    assert rec.drift == []
