"""Version metadata behavior (Issue #7382)."""

from __future__ import annotations

import ast
import importlib
import re
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import tasksmith

_REPO_ROOT = Path(__file__).resolve().parents[1]
_INIT_PATH = _REPO_ROOT / "src" / "tasksmith" / "__init__.py"
_SEMVER_LITERAL = re.compile(r"\d+\.\d+\.\d+")


def test_version_matches_installed_metadata() -> None:
    """AC1: __version__ matches importlib.metadata for the installed package."""
    assert tasksmith.__version__ == version("tasksmith")


def test_version_falls_back_when_not_installed(monkeypatch) -> None:
    """AC2: reload uses 0+unknown when package metadata is missing."""

    def _missing(_name: str) -> str:
        raise PackageNotFoundError(_name)

    monkeypatch.setattr("importlib.metadata.version", _missing)
    reloaded = importlib.reload(tasksmith)
    assert reloaded.__version__ == "0+unknown"
    monkeypatch.undo()
    importlib.reload(tasksmith)


def test_init_has_no_hardcoded_version() -> None:
    """AC3: __init__.py has no semver-like string literal constants."""
    tree = ast.parse(_INIT_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert not _SEMVER_LITERAL.search(node.value), (
                f"hardcoded semver-like literal in __init__.py: {node.value!r}"
            )
