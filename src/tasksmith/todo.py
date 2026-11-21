"""todo.md helpers: list registration candidates, register lines, and report drift."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from tasksmith.body import build_controlled_notes
from tasksmith.model import FLOW_DAG, FLOW_PERSONAL, OwnerType, PipelineState
from tasksmith.store import TaskNotFoundError, TaskStore, TaskStoreError

__all__ = [
    "RegisterResult",
    "candidates",
    "find_drift",
    "line_id",
    "register",
]

_MARK_RE = re.compile(r"<!--\s*task:(\S+?)\s*-->")
_LINE_RE = re.compile(r"^- \[([ xX])\] (.+)$")


def line_id(line: str) -> str:
    """Stable id for a todo line (full line without trailing newline)."""
    return hashlib.sha1(line.encode("utf-8")).hexdigest()[:12]


def _line_text(body: str) -> str:
    return _MARK_RE.sub("", body).strip()


def _owner_type(raw: object) -> OwnerType:
    if isinstance(raw, str):
        try:
            return OwnerType(raw)
        except ValueError:
            pass
    return OwnerType.AI


def _atomic_write(path: Path, content: str) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".todo-", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp, path)
    except Exception:
        with suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def candidates(todo_text: str) -> list[dict]:
    """Return open, unmarked todo lines in file order."""
    out: list[dict] = []
    for lineno, raw in enumerate(todo_text.splitlines(), start=1):
        m = _LINE_RE.match(raw)
        if not m or m.group(1) != " ":
            continue
        if _MARK_RE.search(raw):
            continue
        out.append(
            {
                "line_id": line_id(raw),
                "text": m.group(2).strip(),
                "line_no": lineno,
            }
        )
    return out


@dataclass
class RegisterResult:
    registered: int
    skipped: int
    failed: int
    skipped_reasons: list[str]
    failures: list[str]


def register(
    todo_path: Path,
    decisions: list[dict],
    *,
    project_id: str | None,
    store: TaskStore,
) -> RegisterResult:
    registered = 0
    skipped = 0
    failed = 0
    skipped_reasons: list[str] = []
    failures: list[str] = []

    for dec in decisions:
        line_id_val = dec.get("line_id")
        if not dec.get("register"):
            skipped += 1
            reason = dec.get("reason") or "no reason"
            skipped_reasons.append(f"{line_id_val}: {reason}")
            continue

        if not isinstance(line_id_val, str):
            skipped += 1
            skipped_reasons.append(f"{line_id_val}: line not found")
            continue

        todo_text = todo_path.read_text(encoding="utf-8") if todo_path.exists() else ""
        lines = todo_text.splitlines(keepends=True)

        target_idx: int | None = None
        target_line: str | None = None
        for idx, piece in enumerate(lines):
            bare = piece.rstrip("\n")
            if line_id(bare) == line_id_val:
                target_idx = idx
                target_line = bare
                break

        if target_line is None or target_idx is None:
            skipped += 1
            skipped_reasons.append(f"{line_id_val}: line not found")
            continue

        if _MARK_RE.search(target_line):
            skipped += 1
            skipped_reasons.append(f"{line_id_val}: already registered")
            continue

        m = _LINE_RE.match(target_line)
        if not m or m.group(1) != " ":
            skipped += 1
            skipped_reasons.append(f"{line_id_val}: not open")
            continue

        text = m.group(2).strip()
        owner = _owner_type(dec.get("owner"))
        section = dec.get("section")
        section_gid = section if isinstance(section, str) and section else None
        flow = FLOW_PERSONAL if owner is OwnerType.HUMAN else FLOW_DAG
        body = build_controlled_notes(
            objective=text,
            owner=owner,
            state=PipelineState.REVIEW,
            flow=flow,
        )

        try:
            task_gid = store.create_task(
                text,
                project_gid=project_id,
                section_gid=section_gid,
                body=body,
            )
        except TaskStoreError as exc:
            failed += 1
            failures.append(f"{line_id_val}: {exc}")
            continue

        stamped = target_line + f" <!-- task:{task_gid} -->"
        had_nl = lines[target_idx].endswith("\n")
        lines[target_idx] = stamped + ("\n" if had_nl else "")
        _atomic_write(todo_path, "".join(lines))
        registered += 1

    return RegisterResult(
        registered=registered,
        skipped=skipped,
        failed=failed,
        skipped_reasons=skipped_reasons,
        failures=failures,
    )


def _drift_item(
    *,
    kind: str,
    task_id: str,
    line: str,
    task_name: str | None,
) -> dict:
    return {
        "kind": kind,
        "task_id": task_id,
        "line": line,
        "task_name": task_name,
    }


def find_drift(todo_text: str, store: TaskStore) -> list[dict]:
    """Compare marked todo lines with tasks; report only, never write."""
    drifts: list[dict] = []

    for line in todo_text.splitlines():
        m = _LINE_RE.match(line)
        if not m:
            continue
        mark = _MARK_RE.search(line)
        if not mark:
            continue

        task_id = mark.group(1)
        todo_checked = m.group(1).lower() == "x"
        line_text = _line_text(m.group(2))

        try:
            task = store.get_task(task_id)
        except TaskNotFoundError:
            drifts.append(
                _drift_item(
                    kind="task_missing",
                    task_id=task_id,
                    line=line,
                    task_name=None,
                )
            )
            continue

        task_name = (task.get("name") or "").strip()
        task_completed = bool(task.get("completed"))

        kinds: list[str] = []
        if todo_checked and not task_completed:
            kinds.append("checked_in_todo")
        elif not todo_checked and task_completed:
            kinds.append("unchecked_in_todo")
        if line_text != task_name:
            kinds.append("text_changed")

        for kind in kinds:
            drifts.append(
                _drift_item(
                    kind=kind,
                    task_id=task_id,
                    line=line,
                    task_name=task_name,
                )
            )

    return drifts
