"""Behavior tests for LocalTaskStore. See Issue #7514."""

from __future__ import annotations

import json

from tasksmith import LocalTaskStore


def test_subtasks_are_nested_in_numeric_id_order(tmp_path) -> None:
    """AC1: list_tasks returns parents with direct children in numeric gid order."""
    store = LocalTaskStore(tmp_path)
    parent = store.create_task("P", project_gid="p1")
    store.create_subtask(parent, "C1", project_gid="p1")
    store.create_subtask(parent, "C2", project_gid="p1")
    store.create_task("Other project", project_gid="p2")

    listed = store.list_tasks("p1")
    assert len(listed) == 1
    assert listed[0]["gid"] == parent
    children = listed[0]["subtasks"]
    assert len(children) == 2
    assert [int(item["gid"]) for item in children] == sorted(int(item["gid"]) for item in children)
    assert "subtasks" not in children[0]
    assert "subtasks" not in children[1]


def test_parent_reads_latest_child_state(tmp_path) -> None:
    """AC2: parent subtasks reflect the latest on-disk child state."""
    store = LocalTaskStore(tmp_path)
    parent = store.create_task("P", project_gid="p1")
    child = store.create_subtask(parent, "C", project_gid="p1")
    store.update_task(child, completed=True, notes="updated")

    parent_view = store.get_task(parent)
    nested = next(row for row in parent_view["subtasks"] if row["gid"] == child)
    assert nested["completed"] is True
    assert nested["notes"] == "updated"


def test_section_gid_round_trip(tmp_path) -> None:
    """AC3: section_gid is stored, returned, and updated on tasks."""
    store = LocalTaskStore(tmp_path)
    with_section = store.create_task("A", project_gid="p1", section_gid="s1")
    plain = store.create_task("B", project_gid="p1")

    assert store.get_task(with_section)["section_gid"] == "s1"
    assert store.get_task(plain)["section_gid"] is None

    store.update_task(with_section, section_gid="s2")
    assert store.get_task(with_section)["section_gid"] == "s2"


def test_new_task_does_not_create_tag_storage(tmp_path) -> None:
    """AC4: new tasks never expose tags or create tags.json."""
    store = LocalTaskStore(tmp_path)
    task_id = store.create_task("T", project_gid="p1")
    store.update_task(task_id, notes="n")

    task = store.get_task(task_id)
    assert "tags" not in task
    for child in task.get("subtasks", []):
        assert "tags" not in child
    assert not (tmp_path / ".tasks" / "tags.json").is_file()


def test_legacy_tag_data_is_ignored_and_preserved(tmp_path) -> None:
    """AC5: legacy tags on disk are omitted publicly but left untouched on update."""
    tasks_dir = tmp_path / ".tasks" / "tasks"
    tasks_dir.mkdir(parents=True)
    tags_path = tmp_path / ".tasks" / "tags.json"
    tags_path.write_text(json.dumps([{"gid": "9", "name": "state:READY"}]) + "\n", encoding="utf-8")
    legacy = {
        "gid": "1",
        "name": "Legacy",
        "notes": "",
        "html_notes": "",
        "completed": False,
        "due_on": None,
        "project": "p1",
        "section": None,
        "parent": None,
        "assignee": None,
        "tags": ["9"],
        "created_at": "2026-01-01T00:00:00Z",
        "modified_at": "2026-01-01T00:00:00Z",
    }
    (tasks_dir / "1.json").write_text(json.dumps(legacy, indent=2) + "\n", encoding="utf-8")
    (tmp_path / ".tasks" / "counter").write_text("1\n", encoding="utf-8")

    store = LocalTaskStore(tmp_path)
    public = store.get_task("1")
    assert "tags" not in public

    store.update_task("1", name="renamed")

    stored = json.loads((tasks_dir / "1.json").read_text(encoding="utf-8"))
    assert stored["tags"] == ["9"]
    assert tags_path.read_text(encoding="utf-8") == json.dumps([{"gid": "9", "name": "state:READY"}]) + "\n"
