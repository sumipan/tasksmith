"""todo.md registration and drift. Refs: issue #7531."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from tasksmith.local import LocalTaskStore
from tasksmith.todo import candidates, find_drift, line_id, register

WORKTREE = Path(__file__).resolve().parents[1]

TODO_LINES = (
    "- [ ] alpha\n"
    "- [ ] beta\n"
    "- [x] gamma\n"
    "- [ ] delta <!-- task:1 -->\n"
)


def _write_todo(path: Path, text: str = TODO_LINES) -> None:
    path.write_text(text, encoding="utf-8")


def test_candidates_returns_open_unmarked_lines(tmp_path: Path) -> None:
    """Refs: #7531 AC1."""
    todo = tmp_path / "todo.md"
    _write_todo(todo)
    got = candidates(todo.read_text(encoding="utf-8"))
    assert len(got) == 2
    assert got[0]["text"] == "alpha"
    assert got[0]["line_no"] == 1
    assert got[0]["line_id"] == hashlib.sha1(b"- [ ] alpha").hexdigest()[:12]
    assert got[1]["text"] == "beta"
    assert got[1]["line_no"] == 2
    assert got[1]["line_id"] == hashlib.sha1(b"- [ ] beta").hexdigest()[:12]


def test_register_creates_task_and_marks_line(tmp_path: Path) -> None:
    """Refs: #7531 AC2."""
    store = LocalTaskStore(tmp_path)
    todo = tmp_path / "todo.md"
    _write_todo(todo)
    alpha_id = line_id("- [ ] alpha")
    beta_id = line_id("- [ ] beta")
    decisions = [
        {"line_id": alpha_id, "register": True, "owner": "HUMAN"},
        {"line_id": beta_id, "register": False, "reason": "duplicate"},
    ]
    before = len(store.list_tasks())
    result = register(todo, decisions, project_id=None, store=store)
    assert result.registered == 1
    assert result.skipped == 1
    assert result.failed == 0
    assert any("duplicate" in reason for reason in result.skipped_reasons)
    assert len(store.list_tasks()) == before + 1
    task = store.list_tasks()[0]
    assert task["name"] == "alpha"
    assert "- flow: personal" in task["body"]
    assert "- state: REVIEW" in task["body"]
    alpha_line = todo.read_text(encoding="utf-8").splitlines()[0]
    assert f"<!-- task:{task['gid']} -->" in alpha_line
    beta_line = todo.read_text(encoding="utf-8").splitlines()[1]
    assert "<!-- task:" not in beta_line


def test_register_twice_does_not_duplicate(tmp_path: Path) -> None:
    """Refs: #7531 AC3."""
    store = LocalTaskStore(tmp_path)
    todo = tmp_path / "todo.md"
    _write_todo(todo)
    alpha_id = line_id("- [ ] alpha")
    beta_id = line_id("- [ ] beta")
    decisions = [
        {"line_id": alpha_id, "register": True, "owner": "HUMAN"},
        {"line_id": beta_id, "register": False, "reason": "duplicate"},
    ]
    register(todo, decisions, project_id=None, store=store)
    count_after_first = len(store.list_tasks())
    second = register(todo, decisions, project_id=None, store=store)
    assert second.registered == 0
    assert len(store.list_tasks()) == count_after_first


def _register_alpha(tmp_path: Path) -> tuple[LocalTaskStore, Path, str]:
    store = LocalTaskStore(tmp_path)
    todo = tmp_path / "todo.md"
    _write_todo(todo, "- [ ] alpha\n")
    alpha_id = line_id("- [ ] alpha")
    register(
        todo,
        [{"line_id": alpha_id, "register": True, "owner": "HUMAN"}],
        project_id=None,
        store=store,
    )
    task_gid = store.list_tasks()[0]["gid"]
    return store, todo, task_gid


def test_drift_checked_in_todo(tmp_path: Path) -> None:
    """Refs: #7531 AC4."""
    store, todo, task_gid = _register_alpha(tmp_path)
    lines = todo.read_text(encoding="utf-8").splitlines()
    lines[0] = lines[0].replace("- [ ]", "- [x]", 1)
    todo.write_text("\n".join(lines) + "\n", encoding="utf-8")
    drifts = find_drift(todo.read_text(encoding="utf-8"), store)
    checked = [d for d in drifts if d["kind"] == "checked_in_todo"]
    assert len(checked) == 1
    assert checked[0]["task_id"] == task_gid


def test_drift_unchecked_in_todo(tmp_path: Path) -> None:
    """Refs: #7531 AC5."""
    store, todo, task_gid = _register_alpha(tmp_path)
    store.update_task(task_gid, completed=True)
    drifts = find_drift(todo.read_text(encoding="utf-8"), store)
    unchecked = [d for d in drifts if d["kind"] == "unchecked_in_todo"]
    assert len(unchecked) == 1
    assert unchecked[0]["task_id"] == task_gid


def test_drift_text_changed(tmp_path: Path) -> None:
    """Refs: #7531 AC6."""
    store, todo, task_gid = _register_alpha(tmp_path)
    lines = todo.read_text(encoding="utf-8").splitlines()
    lines[0] = lines[0].replace("alpha", "alpha2", 1)
    todo.write_text("\n".join(lines) + "\n", encoding="utf-8")
    drifts = find_drift(todo.read_text(encoding="utf-8"), store)
    changed = [d for d in drifts if d["kind"] == "text_changed"]
    assert len(changed) == 1
    assert changed[0]["task_id"] == task_gid
    assert changed[0]["task_name"] == "alpha"


def test_drift_task_missing(tmp_path: Path) -> None:
    """Refs: #7531 AC7."""
    store, todo, task_gid = _register_alpha(tmp_path)
    task_file = tmp_path / ".tasks" / "tasks" / f"{task_gid}.json"
    task_file.unlink()
    drifts = find_drift(todo.read_text(encoding="utf-8"), store)
    missing = [d for d in drifts if d["kind"] == "task_missing"]
    assert len(missing) == 1
    assert missing[0]["task_id"] == task_gid
    assert missing[0]["task_name"] is None


def test_drift_does_not_write(tmp_path: Path) -> None:
    """Refs: #7531 AC8."""
    store, todo, _task_gid = _register_alpha(tmp_path)
    lines = todo.read_text(encoding="utf-8").splitlines()
    lines[0] = lines[0].replace("- [ ]", "- [x]", 1)
    todo.write_text("\n".join(lines) + "\n", encoding="utf-8")
    todo_before = todo.read_text(encoding="utf-8")
    tasks_dir = tmp_path / ".tasks" / "tasks"
    task_snap = {p.name: p.read_text(encoding="utf-8") for p in tasks_dir.glob("*.json")}
    find_drift(todo.read_text(encoding="utf-8"), store)
    assert todo.read_text(encoding="utf-8") == todo_before
    after = {p.name: p.read_text(encoding="utf-8") for p in tasks_dir.glob("*.json")}
    assert after == task_snap


def _cli_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(WORKTREE / "src")
    return env


def test_cli_todo_drift_outputs_json_array(tmp_path: Path) -> None:
    """Refs: #7531 AC9."""
    root = tmp_path / "cli-root"
    todo = tmp_path / "marked.md"
    _write_todo(todo)
    store = LocalTaskStore(root)
    alpha_id = line_id("- [ ] alpha")
    register(todo, [{"line_id": alpha_id, "register": True, "owner": "AI"}], project_id=None, store=store)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasksmith",
            "--root",
            str(root),
            "todo",
            "drift",
            str(todo),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_cli_env(),
        cwd=WORKTREE,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert isinstance(payload, list)


def test_cli_todo_register_outputs_result_json(tmp_path: Path) -> None:
    """Refs: #7531 AC10."""
    root = tmp_path / "cli-root"
    todo = tmp_path / "reg.md"
    _write_todo(todo, "- [ ] solo\n")
    decisions = tmp_path / "decisions.json"
    solo_id = line_id("- [ ] solo")
    decisions.write_text(
        json.dumps([{"line_id": solo_id, "register": True, "owner": "AI"}]),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasksmith",
            "--root",
            str(root),
            "todo",
            "register",
            str(todo),
            "--decisions",
            str(decisions),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_cli_env(),
        cwd=WORKTREE,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["registered"] == 1
    assert "<!-- task:" in todo.read_text(encoding="utf-8")
