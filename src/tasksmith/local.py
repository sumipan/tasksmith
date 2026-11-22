"""Local file-backed TaskStore implementation."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from tasksmith.store import TaskNotFoundError, TaskStoreError


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _raw_body(raw: dict) -> str:
    if "body" in raw:
        return raw.get("body") or ""
    html_notes = raw.get("html_notes")
    if isinstance(html_notes, str) and html_notes:
        return html_notes
    notes = raw.get("notes")
    if isinstance(notes, str) and notes:
        return notes
    return ""


class LocalTaskStore:
    """TaskStore backed by ``<root>/.tasks/`` on disk."""

    def __init__(self, root: Path | str, *, actor_name: str = "tasksmith-local") -> None:
        self._root = Path(root)
        self._actor_name = actor_name
        self._tasks_root = self._root / ".tasks"
        self._tasks_dir = self._tasks_root / "tasks"
        self._counter_path = self._tasks_root / "counter"
        self._lock_path = self._tasks_root / "lock"

    def ai_actor_names(self) -> frozenset[str]:
        return frozenset({self._actor_name})

    def get_task(self, task_gid: str) -> dict:
        raw = self._read_raw_task(task_gid)
        return self._public_task(task_gid, raw, include_subtasks=not raw.get("parent"))

    def list_tasks(self, project_gid: str | None = None) -> list[dict]:
        if not self._tasks_dir.is_dir():
            return []
        items: list[dict] = []
        for path in self._tasks_dir.glob("*.json"):
            task_gid = path.stem
            raw = self._read_raw_task(task_gid)
            if raw.get("parent"):
                continue
            if project_gid is not None and raw.get("project") != project_gid:
                continue
            items.append(self._public_task(task_gid, raw, include_subtasks=True))
        items.sort(key=lambda row: int(row["gid"]))
        return items

    def get_task_stories(self, task_gid: str) -> list[dict]:
        self._require_task_file(task_gid)
        path = self._stories_path(task_gid)
        if not path.is_file():
            return []
        stories: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                stories.append(json.loads(line))
        return stories

    def create_task(
        self,
        name: str,
        *,
        project_gid: str | None = None,
        due_on: str | None = None,
        section_gid: str | None = None,
        body: str | None = None,
        assign_owner: str | None = None,
    ) -> str:
        del assign_owner
        return self._create_task_record(
            name,
            parent=None,
            project_gid=project_gid,
            due_on=due_on,
            section_gid=section_gid,
            body=body,
        )

    def create_subtask(
        self,
        parent_gid: str,
        name: str,
        *,
        project_gid: str | None = None,
        due_on: str | None = None,
        section_gid: str | None = None,
        body: str | None = None,
        assign_owner: str | None = None,
    ) -> str:
        del assign_owner
        self._require_task_file(parent_gid)
        return self._create_task_record(
            name,
            parent=parent_gid,
            project_gid=project_gid,
            due_on=due_on,
            section_gid=section_gid,
            body=body,
        )

    def update_task(
        self,
        task_gid: str,
        *,
        name: str | None = None,
        due_on: str | None = None,
        section_gid: str | None = None,
        body: str | None = None,
        completed: bool | None = None,
    ) -> None:
        with self._locked():
            raw = self._read_raw_task(task_gid)
            description_changed = False
            if body is not None and body != _raw_body(raw):
                raw["body"] = body
                description_changed = True
            if name is not None:
                raw["name"] = name
            if due_on is not None:
                raw["due_on"] = due_on
            if section_gid is not None:
                raw["section"] = section_gid
            if completed is not None:
                if completed:
                    if not raw.get("completed"):
                        raw["completed_at"] = _utcnow()
                    raw["completed"] = True
                else:
                    raw["completed"] = False
                    raw["completed_at"] = None
            raw["modified_at"] = _utcnow()
            self._write_raw_task(task_gid, raw)
            if description_changed:
                self._append_story_locked(
                    task_gid,
                    story_type="system",
                    text="changed the description",
                )

    def add_comment(self, task_gid: str, text: str) -> None:
        with self._locked():
            self._require_task_file(task_gid)
            self._append_story_locked(task_gid, story_type="comment", text=text)

    def _create_task_record(
        self,
        name: str,
        *,
        parent: str | None,
        project_gid: str | None,
        due_on: str | None,
        section_gid: str | None,
        body: str | None,
    ) -> str:
        with self._locked():
            task_gid = self._allocate_id_locked()
            now = _utcnow()
            record = {
                "gid": task_gid,
                "name": name,
                "body": body if body is not None else "",
                "completed": False,
                "completed_at": None,
                "due_on": due_on,
                "project": project_gid,
                "section": section_gid,
                "parent": parent,
                "assignee": None,
                "created_at": now,
                "modified_at": now,
            }
            self._write_raw_task(task_gid, record)
            return task_gid

    def _public_task(self, task_gid: str, raw: dict, *, include_subtasks: bool) -> dict:
        parent = raw.get("parent")
        parent_pub = {"gid": parent} if parent else None
        path = self._task_path(task_gid)
        completed = bool(raw.get("completed"))
        completed_at: str | None
        if completed:
            completed_at = raw.get("completed_at") or raw.get("modified_at")
        else:
            completed_at = None
        public: dict[str, Any] = {
            "gid": task_gid,
            "name": raw.get("name", ""),
            "completed": completed,
            "completed_at": completed_at,
            "due_on": raw.get("due_on"),
            "body": _raw_body(raw),
            "permalink_url": path.resolve().as_uri(),
            "parent": parent_pub,
            "section_gid": raw.get("section"),
        }
        if include_subtasks:
            public["subtasks"] = self._direct_subtasks(task_gid)
        return public

    def _direct_subtasks(self, parent_gid: str) -> list[dict]:
        if not self._tasks_dir.is_dir():
            return []
        children: list[dict] = []
        for path in self._tasks_dir.glob("*.json"):
            child_gid = path.stem
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("parent") != parent_gid:
                continue
            children.append(self._public_task(child_gid, raw, include_subtasks=False))
        children.sort(key=lambda row: int(row["gid"]))
        return children

    def _task_path(self, task_gid: str) -> Path:
        return self._tasks_dir / f"{task_gid}.json"

    def _stories_path(self, task_gid: str) -> Path:
        return self._tasks_dir / f"{task_gid}.stories.jsonl"

    def _require_task_file(self, task_gid: str) -> None:
        if not self._task_path(task_gid).is_file():
            raise TaskNotFoundError(task_gid)

    def _read_raw_task(self, task_gid: str) -> dict:
        self._require_task_file(task_gid)
        return json.loads(self._task_path(task_gid).read_text(encoding="utf-8"))

    def _write_raw_task(self, task_gid: str, data: dict) -> None:
        path = self._task_path(task_gid)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_json(path, data)

    def _write_json(self, path: Path, data: Any) -> None:
        self._write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    def _write_text_atomic(self, path: Path, content: str) -> None:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            os.write(fd, content.encode("utf-8"))
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(tmp, str(path))
        except BaseException:
            if fd >= 0:
                os.close(fd)
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def _append_story_locked(self, task_gid: str, *, story_type: str, text: str) -> None:
        story = {
            "gid": self._allocate_id_locked(),
            "type": story_type,
            "text": text,
            "created_at": _utcnow(),
            "created_by": {"name": self._actor_name},
        }
        path = self._stories_path(task_gid)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(story, ensure_ascii=False) + "\n")

    def _allocate_id_locked(self) -> str:
        self._tasks_root.mkdir(parents=True, exist_ok=True)
        raw = self._counter_path.read_text(encoding="utf-8").strip() if self._counter_path.exists() else "0"
        current = int(raw or "0")
        nxt = current + 1
        self._write_text_atomic(self._counter_path, f"{nxt}\n")
        return str(nxt)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self._tasks_root.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 30.0
        fd = -1
        while True:
            try:
                fd = os.open(str(self._lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                if time.monotonic() > deadline:
                    raise TaskStoreError(f"timed out waiting for lock at {self._lock_path}")
                time.sleep(0.01)
        try:
            yield
        finally:
            if fd >= 0:
                os.close(fd)
            with contextlib.suppress(FileNotFoundError):
                self._lock_path.unlink()
