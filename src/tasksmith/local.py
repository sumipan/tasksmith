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


class LocalTaskStore:
    """TaskStore backed by ``<root>/.tasks/`` on disk."""

    def __init__(self, root: Path | str, *, actor_name: str = "tasksmith-local") -> None:
        self._root = Path(root)
        self._actor_name = actor_name
        self._tasks_root = self._root / ".tasks"
        self._tasks_dir = self._tasks_root / "tasks"
        self._tags_path = self._tasks_root / "tags.json"
        self._counter_path = self._tasks_root / "counter"
        self._lock_path = self._tasks_root / "lock"

    def default_client(self) -> Any:
        return None

    def ai_actor_names(self, client: Any = None) -> frozenset[str]:
        return frozenset({self._actor_name})

    def get_task(
        self,
        task_gid: str,
        *,
        opt_fields: str | None = None,
        client: Any = None,
    ) -> dict:
        del opt_fields, client
        raw = self._read_raw_task(task_gid)
        return self._public_task(task_gid, raw)

    def list_tasks(
        self,
        project_gid: str | None = None,
        *,
        opt_fields: str | None = None,
        client: Any = None,
    ) -> list[dict]:
        del opt_fields, client
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
            items.append(self._public_task(task_gid, raw))
        items.sort(key=lambda row: int(row["gid"]))
        return items

    def get_task_stories(self, task_gid: str, *, client: Any = None) -> list[dict]:
        del client
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
        notes: str | None = None,
        html_notes: str | None = None,
        assign_owner: str | None = None,
        client: Any = None,
    ) -> str:
        del assign_owner, client
        return self._create_task_record(
            name,
            parent=None,
            project_gid=project_gid,
            due_on=due_on,
            section_gid=section_gid,
            notes=notes,
            html_notes=html_notes,
        )

    def create_subtask(
        self,
        parent_gid: str,
        name: str,
        *,
        project_gid: str | None = None,
        due_on: str | None = None,
        section_gid: str | None = None,
        notes: str | None = None,
        html_notes: str | None = None,
        assign_owner: str | None = None,
        client: Any = None,
    ) -> str:
        del assign_owner, client
        self._require_task_file(parent_gid)
        return self._create_task_record(
            name,
            parent=parent_gid,
            project_gid=project_gid,
            due_on=due_on,
            section_gid=section_gid,
            notes=notes,
            html_notes=html_notes,
        )

    def update_task(
        self,
        task_gid: str,
        *,
        name: str | None = None,
        due_on: str | None = None,
        notes: str | None = None,
        html_notes: str | None = None,
        completed: bool | None = None,
        assignee: str | None = None,
        custom_fields: dict | None = None,
        client: Any = None,
    ) -> None:
        del custom_fields, client
        with self._locked():
            raw = self._read_raw_task(task_gid)
            description_changed = False
            if notes is not None and notes != raw.get("notes", ""):
                raw["notes"] = notes
                description_changed = True
            if html_notes is not None and html_notes != raw.get("html_notes", ""):
                raw["html_notes"] = html_notes
                description_changed = True
            if name is not None:
                raw["name"] = name
            if due_on is not None:
                raw["due_on"] = due_on
            if completed is not None:
                raw["completed"] = completed
            if assignee is not None:
                raw["assignee"] = assignee
            raw["modified_at"] = _utcnow()
            self._write_raw_task(task_gid, raw)
            if description_changed:
                self._append_story_locked(
                    task_gid,
                    story_type="system",
                    text="changed the description",
                )

    def add_comment(self, task_gid: str, text: str, *, client: Any = None) -> None:
        del client
        with self._locked():
            self._require_task_file(task_gid)
            self._append_story_locked(task_gid, story_type="comment", text=text)

    def list_workspace_tags(self, workspace_gid: str, *, client: Any = None) -> list[dict]:
        del workspace_gid, client
        if not self._tags_path.is_file():
            return []
        return json.loads(self._tags_path.read_text(encoding="utf-8"))

    def create_tag(
        self,
        workspace_gid: str,
        *,
        name: str,
        client: Any = None,
    ) -> dict:
        del workspace_gid, client
        with self._locked():
            tags = self._load_tags_locked()
            tag = {"gid": self._allocate_id_locked(), "name": name}
            tags.append(tag)
            self._save_tags_locked(tags)
            return dict(tag)

    def add_tag_to_task(self, task_gid: str, *, tag_gid: str, client: Any = None) -> None:
        del client
        with self._locked():
            raw = self._read_raw_task(task_gid)
            tags = self._load_tags_locked()
            known = {row["gid"] for row in tags}
            if tag_gid not in known:
                raise TaskStoreError(f"unknown tag id: {tag_gid}")
            tag_ids: list[str] = list(raw.get("tags") or [])
            if tag_gid not in tag_ids:
                tag_ids.append(tag_gid)
                raw["tags"] = tag_ids
                raw["modified_at"] = _utcnow()
                self._write_raw_task(task_gid, raw)

    def remove_tag_from_task(self, task_gid: str, *, tag_gid: str, client: Any = None) -> None:
        del client
        with self._locked():
            if not self._task_path(task_gid).is_file():
                raise TaskNotFoundError(task_gid)
            tags = self._load_tags_locked()
            known = {row["gid"] for row in tags}
            if tag_gid not in known:
                raise TaskStoreError(f"unknown tag id: {tag_gid}")
            raw = self._read_raw_task(task_gid)
            tag_ids = [gid for gid in (raw.get("tags") or []) if gid != tag_gid]
            if tag_ids == (raw.get("tags") or []):
                return
            raw["tags"] = tag_ids
            raw["modified_at"] = _utcnow()
            self._write_raw_task(task_gid, raw)

    def _create_task_record(
        self,
        name: str,
        *,
        parent: str | None,
        project_gid: str | None,
        due_on: str | None,
        section_gid: str | None,
        notes: str | None,
        html_notes: str | None,
    ) -> str:
        with self._locked():
            task_gid = self._allocate_id_locked()
            now = _utcnow()
            record = {
                "gid": task_gid,
                "name": name,
                "notes": notes if notes is not None else "",
                "html_notes": html_notes if html_notes is not None else "",
                "completed": False,
                "due_on": due_on,
                "project": project_gid,
                "section": section_gid,
                "parent": parent,
                "assignee": None,
                "tags": [],
                "created_at": now,
                "modified_at": now,
            }
            self._write_raw_task(task_gid, record)
            return task_gid

    def _public_task(self, task_gid: str, raw: dict) -> dict:
        tag_lookup = {row["gid"]: row["name"] for row in self._load_tags_readonly()}
        public_tags = [
            {"gid": gid, "name": tag_lookup[gid]}
            for gid in (raw.get("tags") or [])
            if gid in tag_lookup
        ]
        parent = raw.get("parent")
        parent_pub = {"gid": parent} if parent else None
        path = self._task_path(task_gid)
        return {
            "gid": task_gid,
            "name": raw.get("name", ""),
            "completed": bool(raw.get("completed")),
            "due_on": raw.get("due_on"),
            "notes": raw.get("notes", ""),
            "html_notes": raw.get("html_notes", ""),
            "permalink_url": path.resolve().as_uri(),
            "tags": public_tags,
            "parent": parent_pub,
        }

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

    def _load_tags_readonly(self) -> list[dict]:
        if not self._tags_path.is_file():
            return []
        return json.loads(self._tags_path.read_text(encoding="utf-8"))

    def _load_tags_locked(self) -> list[dict]:
        self._tasks_root.mkdir(parents=True, exist_ok=True)
        if not self._tags_path.is_file():
            return []
        return json.loads(self._tags_path.read_text(encoding="utf-8"))

    def _save_tags_locked(self, tags: list[dict]) -> None:
        self._tasks_root.mkdir(parents=True, exist_ok=True)
        self._write_json(self._tags_path, tags)

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
