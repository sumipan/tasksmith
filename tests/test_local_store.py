"""Behavior tests for LocalTaskStore. See Issue #7514 and #7518."""

from __future__ import annotations

import json

import pytest

from tasksmith import LocalTaskStore, TaskNotFoundError, TaskStoreError


def test_create_get_update_round_trip(tmp_path) -> None:
    """Creating and updating a task persists its public fields on disk."""
    store = LocalTaskStore(tmp_path)
    task_id = store.create_task(
        "A",
        project_gid="p1",
        body="<body>x</body>",
        due_on="2026-12-01",
    )

    task = store.get_task(task_id)
    assert task["name"] == "A"
    assert task["body"] == "<body>x</body>"
    assert task["due_on"] == "2026-12-01"
    assert task["completed"] is False

    store.update_task(task_id, completed=True)
    updated = store.get_task(task_id)
    assert updated["completed"] is True
    assert updated["name"] == "A"
    assert updated["body"] == "<body>x</body>"
    assert (tmp_path / ".tasks" / "tasks" / f"{task_id}.json").is_file()


def test_comment_and_description_change_are_recorded_as_stories(tmp_path) -> None:
    """Comments and description edits append stories while other updates do not."""
    store = LocalTaskStore(tmp_path)
    task_id = store.create_task("S", project_gid="p1", body="<body>x</body>")

    store.add_comment(task_id, "hello")
    store.update_task(task_id, body="<body>y</body>")
    stories = store.get_task_stories(task_id)
    assert len(stories) == 2
    assert stories[0]["type"] == "comment"
    assert stories[0]["text"] == "hello"
    assert stories[1]["text"] == "changed the description"
    ai_names = store.ai_actor_names()
    assert stories[0]["created_by"]["name"] in ai_names
    assert stories[1]["created_by"]["name"] in ai_names

    store.update_task(task_id, completed=True)
    assert len(store.get_task_stories(task_id)) == 2


def test_unknown_task_raises_not_found(tmp_path) -> None:
    """Missing tasks consistently surface TaskNotFoundError."""
    store = LocalTaskStore(tmp_path)
    missing = "999999"

    with pytest.raises(TaskNotFoundError):
        store.get_task(missing)
    with pytest.raises(TaskNotFoundError):
        store.update_task(missing, completed=True)
    with pytest.raises(TaskNotFoundError):
        store.add_comment(missing, "nope")
    with pytest.raises(TaskNotFoundError):
        store.get_task_stories(missing)

    assert issubclass(TaskNotFoundError, TaskStoreError)


def test_ids_are_unique_across_two_store_instances(tmp_path) -> None:
    """Store instances sharing a root never reuse IDs."""
    left = LocalTaskStore(tmp_path)
    right = LocalTaskStore(tmp_path)
    ids: list[str] = []
    for _ in range(3):
        ids.append(left.create_task("L"))
        ids.append(right.create_task("R"))
    assert len(ids) == len(set(ids))


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


def test_create_task_stores_body(tmp_path) -> None:
    """AC1 (#7518): create_task with body persists and returns that body."""
    store = LocalTaskStore(tmp_path)
    task_id = store.create_task("t", body="# A")
    assert store.get_task(task_id)["body"] == "# A"


def test_body_change_appends_system_story(tmp_path) -> None:
    """AC2 (#7518): body updates append a system story once per real change."""
    store = LocalTaskStore(tmp_path)
    task_id = store.create_task("t", body="# A")

    store.update_task(task_id, body="# B")
    task = store.get_task(task_id)
    assert task["body"] == "# B"
    stories = store.get_task_stories(task_id)
    assert len(stories) == 1
    assert stories[0]["type"] == "system"

    store.update_task(task_id, body="# B")
    assert len(store.get_task_stories(task_id)) == 1


def test_parent_reads_latest_child_state(tmp_path) -> None:
    """AC3: parent subtasks reflect the latest on-disk child state."""
    store = LocalTaskStore(tmp_path)
    parent = store.create_task("P", project_gid="p1")
    child = store.create_subtask(parent, "C", project_gid="p1")
    store.update_task(child, completed=True, body="updated")

    parent_view = store.get_task(parent)
    nested = next(row for row in parent_view["subtasks"] if row["gid"] == child)
    assert nested["completed"] is True
    assert nested["body"] == "updated"


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
    store.update_task(task_id, body="n")

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


def test_legacy_html_notes_is_read_as_body(tmp_path) -> None:
    """AC4 (#7518): legacy html_notes on disk is exposed as body without rewriting files."""
    tasks_dir = tmp_path / ".tasks" / "tasks"
    tasks_dir.mkdir(parents=True)
    legacy = {
        "gid": "1",
        "name": "Legacy",
        "notes": "",
        "html_notes": "<body>x</body>",
        "completed": False,
        "due_on": None,
        "project": "p1",
        "section": None,
        "parent": None,
        "assignee": None,
        "created_at": "2026-01-01T00:00:00Z",
        "modified_at": "2026-01-01T00:00:00Z",
    }
    task_path = tasks_dir / "1.json"
    task_path.write_text(json.dumps(legacy, indent=2) + "\n", encoding="utf-8")
    (tmp_path / ".tasks" / "counter").write_text("1\n", encoding="utf-8")

    store = LocalTaskStore(tmp_path)
    assert store.get_task("1")["body"] == "<body>x</body>"
    assert task_path.read_text(encoding="utf-8") == json.dumps(legacy, indent=2) + "\n"


def test_legacy_notes_is_read_as_body_when_html_notes_is_empty(tmp_path) -> None:
    """AC5 (#7518): legacy notes is used as body when html_notes is empty."""
    tasks_dir = tmp_path / ".tasks" / "tasks"
    tasks_dir.mkdir(parents=True)
    legacy = {
        "gid": "1",
        "name": "Legacy",
        "notes": "plain",
        "html_notes": "",
        "completed": False,
        "due_on": None,
        "project": "p1",
        "section": None,
        "parent": None,
        "assignee": None,
        "created_at": "2026-01-01T00:00:00Z",
        "modified_at": "2026-01-01T00:00:00Z",
    }
    (tasks_dir / "1.json").write_text(json.dumps(legacy, indent=2) + "\n", encoding="utf-8")
    (tmp_path / ".tasks" / "counter").write_text("1\n", encoding="utf-8")

    store = LocalTaskStore(tmp_path)
    assert store.get_task("1")["body"] == "plain"
