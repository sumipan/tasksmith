"""tasksmith AI node execution via ghdag (LLM + finish shell jobs)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from ghdag.pipeline import AuditContext, InlineOrderBuilder, TaskQueue, wait_for_result
from ghdag.workflow import StepConfig

__all__ = [
    "EngineFailed",
    "EngineUnavailable",
    "Executor",
    "GhdagExecutor",
    "GhdagThinker",
    "Thinker",
    "executor",
    "set_executor",
    "set_thinker",
    "thinker",
]

_executor_override: Executor | None = None
_thinker_override: Thinker | None = None


class EngineUnavailable(Exception):
    """No engine is available now (quota pause); retry later."""


class EngineFailed(Exception):
    """LLM job did not succeed or result file is missing."""


class Thinker(Protocol):
    def ask(self, prompt: str, *, weight: str, timeout: float) -> str: ...


def thinker() -> Thinker:
    if _thinker_override is not None:
        return _thinker_override
    raise RuntimeError(
        "tasksmith Thinker is not configured; call tasksmith.execution.set_thinker()"
    )


def set_thinker(impl: Thinker | None) -> None:
    global _thinker_override
    _thinker_override = impl


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


def _engine_available(engine: str, quota_state_path: Path | None) -> bool:
    if quota_state_path is None:
        return True
    try:
        from ghdag.quota import QuotaGate

        snap = QuotaGate(state_path=quota_state_path).snapshot()
    except Exception:
        return True
    eng = snap.engines.get(engine)
    return eng is None or eng.status == "available"


def _pick_route(
    routes: Sequence[tuple[str, str]],
    *,
    quota_state_path: Path | None,
) -> tuple[str, str]:
    for engine, model in routes:
        if _engine_available(engine, quota_state_path):
            return engine, model
    raise EngineUnavailable("all engines paused for route")


class GhdagThinker:
    """Enqueue a single LLM think step and wait synchronously for its result file."""

    def __init__(
        self,
        *,
        queue_dir: Path,
        state_dir: Path,
        routes: Mapping[str, Sequence[tuple[str, str]]],
        quota_state_path: Path | None = None,
    ) -> None:
        light = routes.get("light") or ()
        heavy = routes.get("heavy") or ()
        if not light or not heavy:
            raise ValueError("routes must include non-empty light and heavy")
        self._queue_dir = Path(queue_dir)
        self._state_dir = Path(state_dir)
        self._routes = {
            "light": tuple(light),
            "heavy": tuple(heavy),
        }
        self._quota_state_path = quota_state_path
        self._queue = TaskQueue(
            state_dir=self._state_dir,
            exec_jsonl_path=self._queue_dir / "exec.jsonl",
            queue_dir=str(self._queue_dir),
        )

    @property
    def routes(self) -> dict[str, tuple[tuple[str, str], ...]]:
        return dict(self._routes)

    def ask(self, prompt: str, *, weight: str, timeout: float) -> str:
        if weight not in self._routes:
            raise ValueError(f"weight must be light or heavy, got {weight!r}")
        engine, model = _pick_route(
            self._routes[weight],
            quota_state_path=self._quota_state_path,
        )
        llm_template = prompt.replace("$", "$$")
        steps = [
            StepConfig(
                id="think",
                template=llm_template,
                engine=engine,
                model=model,
                permission="text_only",
            ),
        ]
        records_json = self._queue.enqueue(
            steps,
            {},
            audit_context=AuditContext(source="tasksmith-thinker"),
            order_builder=InlineOrderBuilder(),
        )
        first = json.loads(records_json[0])
        uid = str(first["uuid"])
        result_path = Path(str(first["result_path"]))
        done_dir = self._queue_dir / "done"
        try:
            status, first_line = wait_for_result(done_dir, uid, timeout=timeout)
        except TimeoutError:
            if not _engine_available(engine, self._quota_state_path):
                raise EngineUnavailable(
                    f"engine {engine} paused while waiting for think job"
                ) from None
            raise
        if status != "success":
            raise EngineFailed(f"{status}: {first_line}")
        if not result_path.is_file():
            raise EngineFailed(f"result file missing: {result_path}")
        return result_path.read_text(encoding="utf-8")


