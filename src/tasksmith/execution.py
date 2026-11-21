"""tasksmith AI node execution via ghdag (LLM + finish shell jobs)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from ghdag.pipeline import AuditContext, InlineOrderBuilder, TaskQueue, wait_for_result
from ghdag.workflow import StepConfig

__all__ = [
    "Executor",
    "GhdagExecutor",
    "executor",
    "set_executor",
]

_executor_override: Executor | None = None


class Executor(Protocol):
    def used(self, idempotency_key: str) -> bool: ...

    def submit(
        self, task_id: str, prompt: str, *, idempotency_key: str, meta_digest: str
    ) -> str | None: ...

    def failure(self, idempotency_key: str) -> str | None: ...


def executor() -> Executor:
    if _executor_override is not None:
        return _executor_override
    raise RuntimeError(
        "tasksmith Executor is not configured; call tasksmith.execution.set_executor()"
    )


def set_executor(impl: Executor | None) -> None:
    global _executor_override
    _executor_override = impl


class GhdagExecutor:
    """Enqueue LLM + finish-node shell jobs through ghdag's public pipeline API."""

    def __init__(
        self,
        *,
        queue_dir: Path,
        state_dir: Path,
        route: Sequence[tuple[str, str]],
        finish_template: str,
    ) -> None:
        if not route:
            raise ValueError("route must not be empty")
        self._queue_dir = Path(queue_dir)
        self._state_dir = Path(state_dir)
        self._route = tuple(route)
        self._finish_template = finish_template
        self._queue = TaskQueue(
            state_dir=self._state_dir,
            exec_jsonl_path=self._queue_dir / "exec.jsonl",
            queue_dir=str(self._queue_dir),
        )

    @property
    def route(self) -> tuple[tuple[str, str], ...]:
        return self._route

    def used(self, idempotency_key: str) -> bool:
        return not self._queue.check_idempotency(idempotency_key)

    def submit(
        self,
        task_id: str,
        prompt: str,
        *,
        idempotency_key: str,
        meta_digest: str,
    ) -> str | None:
        if self.used(idempotency_key):
            return None
        engine, model = self._route[0]
        llm_template = prompt.replace("$", "$$")
        steps = [
            StepConfig(
                id="llm",
                template=llm_template,
                engine=engine,
                model=model,
                permission="dangerous_full_access",
            ),
            StepConfig(
                id="finish",
                template=self._finish_template,
                engine="shell",
                model=None,
                depends=["llm"],
            ),
        ]
        base_context = {
            "task_id": task_id,
            "meta_digest": meta_digest,
            "queue_dir": str(self._queue_dir),
        }
        records_json = self._queue.enqueue(
            steps,
            base_context,
            idempotency_key=idempotency_key,
            audit_context=AuditContext(source="tasksmith-tick"),
            metadata={"tasksmith_gid": task_id},
            order_builder=InlineOrderBuilder(),
        )
        first = json.loads(records_json[0])
        return str(first["uuid"])

    def failure(self, idempotency_key: str) -> str | None:
        records = self._queue.find_records_by_idempotency_key(idempotency_key)
        if not records:
            return None
        done_dir = self._queue_dir / "done"
        ordered: list[tuple[str, dict]] = []
        for rec in records:
            step = str((rec.get("annotations") or {}).get("step_name") or "")
            ordered.append((step, rec))
        ordered.sort(key=lambda pair: (0 if pair[0] == "llm" else 1, pair[0]))
        for step, rec in ordered:
            uid = rec.get("uuid")
            if not uid:
                continue
            try:
                status, first_line = wait_for_result(done_dir, str(uid), timeout=0)
            except TimeoutError:
                continue
            if status != "success":
                label = step or "unknown"
                return f"{label}: {first_line}"
        return None


