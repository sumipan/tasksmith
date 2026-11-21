"""File-backed human channel. Refs: issue #7528 AC5."""

from __future__ import annotations

from pathlib import Path

from tasksmith.file_channel import FileChannel


def test_ask_then_follow_up_appends_to_same_request(tmp_path: Path) -> None:
    """Refs: #7528 AC5."""
    ch = FileChannel(tmp_path)
    ref = ch.ask("x")
    assert ref == "1"
    assert (tmp_path / "requests" / "1.md").read_text(encoding="utf-8") == "x"
    ch.follow_up(ref, "y")
    body = (tmp_path / "requests" / "1.md").read_text(encoding="utf-8")
    assert body.endswith("y")
    assert "\n---\n" in body
