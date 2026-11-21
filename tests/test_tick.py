"""Tick and finish-node. See issue #7527 AC6–AC7."""

from __future__ import annotations

from dataclasses import dataclass, field

from tasksmith.body import build_controlled_notes, parse_sections
from tasksmith.execution import set_executor
from tasksmith.model import OwnerType, PipelineState
from tasksmith.tick import finish_node, recover, tick


@dataclass
class RecordingExecutor:
    submits: list[tuple[str, str, str]] = field(default_factory=list)

    def used(self, idempotency_key: str) -> bool:
        return False

    def submit(
        self,
        task_id: str,
        prompt: str,
        *,
        idempotency_key: str,
        meta_digest: str,
    ) -> str | None:
        self.submits.append((task_id, idempotency_key, meta_digest))
        return "job-uuid"

    def failure(self, idempotency_key: str) -> str | None:
        return None


def test_tick_enqueues_ai_node_once(orchestration_env) -> None:
    """Refs: #7527 AC6."""
    store = orchestration_env
    body = build_controlled_notes(
        objective="Run demo skill",
        owner=OwnerType.AI,
        state=PipelineState.READY,
        flow="dag",
        executor="skill:demo",
        acceptance=["Save output to docs/out.md"],
        deliverables="- docs/out.md",
    )
    gid = store.create_task("AI node", body=body)
    recorder = RecordingExecutor()
    set_executor(recorder)

    first = tick()
    assert first.status == "enqueued"
    task = store.get_task(gid)
    secs = parse_sections(task["body"])
    assert secs and "state: RUNNING" in secs["control"]
    assert len(recorder.submits) == 1
    assert recorder.submits[0][0] == gid

    tick()
    assert len(recorder.submits) == 1


def test_finish_node_and_recover(orchestration_env, tmp_path) -> None:
    """Refs: #7527 AC7."""
    store = orchestration_env
    ready_body = build_controlled_notes(
        objective="Run demo skill",
        owner=OwnerType.AI,
        state=PipelineState.READY,
        flow="dag",
        executor="skill:demo",
        acceptance=["Save output to docs/out.md"],
        deliverables="- docs/out.md",
    )
    gid = store.create_task("AI node", body=ready_body)
    recorder = RecordingExecutor()
    set_executor(recorder)
    tick()
    digest = recorder.submits[0][2]

    done_path = tmp_path / "done.md"
    done_path.write_text("ok\nPIPELINE_STATUS: DONE\n", encoding="utf-8")
    fr = finish_node(gid, result_path=done_path, meta_digest=digest)
    assert fr.status == "DONE"
    secs = parse_sections(store.get_task(gid)["body"])
    assert secs and "state: DONE" in secs["control"]

    ready_body2 = build_controlled_notes(
        objective="Run demo skill",
        owner=OwnerType.AI,
        state=PipelineState.READY,
        flow="dag",
        executor="skill:demo",
        acceptance=["Save output to docs/out.md"],
        deliverables="- docs/out.md",
    )
    gid2 = store.create_task("AI blocked", body=ready_body2)
    recorder2 = RecordingExecutor()
    set_executor(recorder2)
    tick()
    digest2 = recorder2.submits[-1][2]
    blocked_path = tmp_path / "blocked.md"
    blocked_path.write_text("fail\nPIPELINE_STATUS: BLOCKED\n", encoding="utf-8")
    fr2 = finish_node(gid2, result_path=blocked_path, meta_digest=digest2)
    assert fr2.status == "BLOCKED"

    rr = recover(gid2)
    assert rr.status == "recovered"
    secs2 = parse_sections(store.get_task(gid2)["body"])
    assert secs2 and "state: READY" in secs2["control"]
