"""File-backed human channel for local tasksmith (stdlib only)."""
from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class FileChannel:
    """HumanChannel implementation using files under ``root``."""

    _SEPARATOR = "\n---\n"

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._lock_path = self._root / "channel.lock"
        self._requests_dir = self._root / "requests"
        self._announcements_path = self._root / "announcements.md"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self._root.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 10.0
        fd = -1
        while True:
            try:
                fd = os.open(
                    str(self._lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY
                )
                break
            except FileExistsError:
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f"timed out waiting for channel lock at {self._lock_path}"
                    )
                time.sleep(0.05)
        try:
            yield
        finally:
            if fd >= 0:
                os.close(fd)
            with contextlib.suppress(FileNotFoundError):
                self._lock_path.unlink()

    def _next_request_id(self) -> int:
        self._requests_dir.mkdir(parents=True, exist_ok=True)
        highest = 0
        for path in self._requests_dir.glob("*.md"):
            stem = path.stem
            if stem.isdigit():
                highest = max(highest, int(stem))
        return highest + 1

    def ask(self, text: str) -> str:
        with self._locked():
            request_id = self._next_request_id()
            ref = str(request_id)
            path = self._requests_dir / f"{ref}.md"
            path.write_text(text, encoding="utf-8")
            return ref

    def follow_up(self, ref: str, text: str) -> None:
        with self._locked():
            path = self._requests_dir / f"{ref}.md"
            if not path.is_file():
                raise FileNotFoundError(ref)
            existing = path.read_text(encoding="utf-8")
            if existing and not existing.endswith("\n"):
                existing += "\n"
            path.write_text(f"{existing}{self._SEPARATOR}{text}", encoding="utf-8")

    def announce(self, text: str) -> None:
        with self._locked():
            self._root.mkdir(parents=True, exist_ok=True)
            if self._announcements_path.is_file():
                existing = self._announcements_path.read_text(encoding="utf-8")
                if existing.strip():
                    chunk = f"{existing.rstrip()}{self._SEPARATOR}{text}"
                else:
                    chunk = text
            else:
                chunk = text
            self._announcements_path.write_text(chunk, encoding="utf-8")

    def link(self, ref: str) -> str | None:
        path = self._requests_dir / f"{ref}.md"
        if not path.is_file():
            return None
        return path.resolve().as_uri()
