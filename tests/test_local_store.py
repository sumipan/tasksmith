"""Behavior tests for LocalTaskStore. See Issue #7328."""

from __future__ import annotations

import pytest

from tasksmith import LocalTaskStore, TaskNotFoundError, TaskStoreError


def test_create_get_update_round_trip(tmp_path) -> None:
  """AC1: create, read, update, and persist a task on disk."""
  store = LocalTaskStore(tmp_path)
  task_id = store.create_task(
      "A",
      project_gid="p1",
      html_notes="<body>x</body>",
      due_on="2026-12-01",
  )
  task = store.get_task(task_id)
  assert task["name"] == "A"
  assert task["html_notes"] == "<body>x</body>"
  assert task["due_on"] == "2026-12-01"
  assert task["completed"] is False

  store.update_task(task_id, completed=True)
  updated = store.get_task(task_id)
  assert updated["completed"] is True
  assert updated["name"] == "A"
  assert updated["html_notes"] == "<body>x</body>"

  task_file = tmp_path / ".tasks" / "tasks" / f"{task_id}.json"
  assert task_file.is_file()


def test_subtask_and_list_tasks(tmp_path) -> None:
  """AC2: subtasks link to parent; list_tasks filters by project and excludes children."""
  store = LocalTaskStore(tmp_path)
  parent = store.create_task("P", project_gid="p1")
  child = store.create_subtask(parent, "C", project_gid="p1")
  other = store.create_task("Other project", project_gid="p2")

  child_task = store.get_task(child)
  assert child_task["parent"]["gid"] == parent

  listed = store.list_tasks("p1")
  ids = [item["gid"] for item in listed]
  assert parent in ids
  assert child not in ids
  assert other not in ids

  numeric = [int(item["gid"]) for item in listed]
  assert numeric == sorted(numeric)


def test_comment_and_description_change_are_recorded_as_stories(tmp_path) -> None:
  """AC3: comments and description edits become stories; other updates do not."""
  store = LocalTaskStore(tmp_path)
  task_id = store.create_task("S", project_gid="p1", html_notes="<body>x</body>")

  store.add_comment(task_id, "hello")
  store.update_task(task_id, html_notes="<body>y</body>")
  stories = store.get_task_stories(task_id)
  assert len(stories) == 2
  assert stories[0]["type"] == "comment"
  assert stories[0]["text"] == "hello"
  assert stories[1]["text"] == "changed the description"
  ai_names = store.ai_actor_names()
  assert stories[0]["created_by"]["name"] in ai_names
  assert stories[1]["created_by"]["name"] in ai_names

  before = len(store.get_task_stories(task_id))
  store.update_task(task_id, completed=True)
  assert len(store.get_task_stories(task_id)) == before


def test_tags_create_add_remove(tmp_path) -> None:
  """AC4: tag lifecycle is reflected on task records."""
  store = LocalTaskStore(tmp_path)
  task_id = store.create_task("T", project_gid="p1")

  tag = store.create_tag("w1", name="state:READY")
  assert "gid" in tag and tag["name"] == "state:READY"
  workspace_tags = store.list_workspace_tags("w1")
  assert any(item["gid"] == tag["gid"] for item in workspace_tags)

  store.add_tag_to_task(task_id, tag_gid=tag["gid"])
  on_task = store.get_task(task_id)["tags"]
  assert any(item["gid"] == tag["gid"] and item["name"] == "state:READY" for item in on_task)

  store.remove_tag_from_task(task_id, tag_gid=tag["gid"])
  after = store.get_task(task_id)["tags"]
  assert not any(item["gid"] == tag["gid"] for item in after)


def test_unknown_task_raises_not_found(tmp_path) -> None:
  """AC5: missing tasks surface TaskNotFoundError (a TaskStoreError)."""
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
  """AC6: concurrent stores sharing a root never reuse IDs."""
  left = LocalTaskStore(tmp_path)
  right = LocalTaskStore(tmp_path)
  ids: list[str] = []
  for _ in range(3):
      ids.append(left.create_task("L"))
      ids.append(right.create_task("R"))
  assert len(ids) == len(set(ids))
