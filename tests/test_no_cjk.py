"""Gate: tracked files must not contain raw CJK (Issue #7328)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

_CJK = re.compile(
    "[%s-%s%s-%s%s-%s]"
    % tuple(map(chr, (0x3000, 0x9FFF, 0xF900, 0xFAFF, 0xFF00, 0xFFEF)))
)


def _tracked_files() -> list[Path] | None:
  if subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=_REPO_ROOT, capture_output=True).returncode != 0:
      return None
  proc = subprocess.run(
      ["git", "ls-files"],
      cwd=_REPO_ROOT,
      capture_output=True,
      text=True,
      check=False,
  )
  if proc.returncode != 0:
      return None
  return [_REPO_ROOT / line for line in proc.stdout.splitlines() if line.strip()]


def test_tracked_files_have_no_cjk() -> None:
  """AC8: git-tracked text files stay CJK-free."""
  paths = _tracked_files()
  if paths is None:
      import pytest

      pytest.skip("not a git worktree or git unavailable")

  violations: list[str] = []
  for path in paths:
      if not path.is_file():
          continue
      try:
          text = path.read_text(encoding="utf-8")
      except (UnicodeDecodeError, OSError):
          continue
      rel = path.relative_to(_REPO_ROOT).as_posix()
      for lineno, line in enumerate(text.splitlines(), start=1):
          if _CJK.search(line):
              violations.append(f"{rel}:{lineno}")
  assert not violations, "CJK found in tracked files:\n" + "\n".join(violations[:80])
