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


def test_intake_seen_unseen_json(tmp_path: Path) -> None:
    """Refs: #7906 AC6."""
    root = tmp_path / "cli-root"
    ledger = root / ".tasks" / "state" / "intake-seen.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps(
            {
                "source_id": "b",
                "outcome": "not_registered",
                "task_gid": None,
                "at": "2026-01-01T00:00:00+00:00",
            }
        )
        + "\n",
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
            "intake-seen",
            "--unseen",
            "a",
            "--unseen",
            "b",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=WORKTREE,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == ["a"]


def test_intake_seen_count_outputs(tmp_path: Path) -> None:
    """Refs: #7906 AC7."""
    root = tmp_path / "cli-root"
    ledger = root / ".tasks" / "state" / "intake-seen.jsonl"
    ledger.parent.mkdir(parents=True)
    rows = [
        json.dumps(
            {
                "source_id": sid,
                "outcome": "not_registered",
                "task_gid": None,
                "at": "2026-01-01T00:00:00+00:00",
            }
        )
        for sid in ("one", "two")
    ]
    ledger.write_text("\n".join(rows) + "\n", encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(WORKTREE / "src")

    plain = subprocess.run(
        [sys.executable, "-m", "tasksmith", "--root", str(root), "intake-seen"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=WORKTREE,
    )
    assert plain.returncode == 0, plain.stderr
    assert plain.stdout.strip() == "2"

    as_json = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasksmith",
            "--root",
            str(root),
            "intake-seen",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=WORKTREE,
    )
    assert as_json.returncode == 0, as_json.stderr
    assert json.loads(as_json.stdout) == {"count": 2}
