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
