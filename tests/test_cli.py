"""CLI with local root. Refs: issue #7528 AC6."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

WORKTREE = Path(__file__).resolve().parents[1]


def test_reconcile_json_with_root(tmp_path: Path) -> None:
    """Refs: #7528 AC6."""
    root = tmp_path / "cli-root"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(WORKTREE / "src")
    proc = subprocess.run(
        [sys.executable, "-m", "tasksmith", "--root", str(root), "reconcile", "--json"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=WORKTREE,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    for key in ("ready", "waiting_human", "blocked", "drift"):
        assert key in payload


def test_intake_and_tags_cli(tmp_path: Path) -> None:
    """Refs: #7778 AC6."""
    root = tmp_path / "cli-root"
    items = tmp_path / "items.json"
    tag = "t"
    items.write_text(
        json.dumps([{"text": "solo", "owner": "AI"}]),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(WORKTREE / "src")

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasksmith",
            "--root",
            str(root),
            "intake",
            "--items",
            str(items),
            "--project",
            "p",
            "--tag",
            tag,
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=WORKTREE,
    )
    assert proc.returncode == 0, proc.stderr
    created = json.loads(proc.stdout)["created"]
    assert len(created) == 1

    from tasksmith.local import LocalTaskStore

    store = LocalTaskStore(root)
    for gid in created:
        store.update_task(gid, completed=True)

    tags = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasksmith",
            "--root",
            str(root),
            "tags",
            "--project",
            "p",
            "--finished-since",
            "2000-01-01",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=WORKTREE,
    )
    assert tags.returncode == 0, tags.stderr
    payload = json.loads(tags.stdout)
    assert any(row["tag"] == tag for row in payload)

    empty = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasksmith",
            "--root",
            str(root),
            "tags",
            "--project",
            "p",
            "--finished-since",
            "2999-01-01",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=WORKTREE,
    )
    assert empty.returncode == 0, empty.stderr
    assert json.loads(empty.stdout) == []
