"""Task body tag metadata and tag-group summaries."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from tasksmith.body import (
    is_controlled_notes,
    parse_meta_field,
    parse_sections,
    remove_meta_field,
    update_meta_field,
)

__all__ = [
    "TagGroup",
    "parse_tags",
    "set_tags",
    "summarize",
]


def _normalize_tag_names(tags: Sequence[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        name = (raw or "").strip()
        if not name:
            raise ValueError("tag name must not be empty")
        if "," in name or "\n" in name or "\r" in name:
            raise ValueError(f"invalid tag name: {raw!r}")
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def parse_tags(body: str | None) -> list[str]:
    """Return tag names from control metadata in written order (deduped, no blanks)."""
    if not body or not is_controlled_notes(body):
        return []
    sections = parse_sections(body)
    if sections is None:
        return []
    value = parse_meta_field(sections, "tags")
    if value is None:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for piece in value.split(","):
        name = piece.strip()
        if not name:
            continue
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def set_tags(body: str, tags: Sequence[str]) -> str:
    """Set the ``tags`` metadata line; remove it when ``tags`` is empty."""
    normalized = _normalize_tag_names(tags)
    if not normalized:
        return remove_meta_field(body, "tags")
    joined = ", ".join(normalized)
    return update_meta_field(body, "tags", joined, create=True)


def _task_state(body: str) -> str | None:
    sections = parse_sections(body)
    if sections is None:
        return None
    return parse_meta_field(sections, "state")


@dataclass
class TagGroup:
    tag: str
    open: list[dict]
    done: list[dict]
    finished: bool
    finished_at: str | None


def summarize(tasks: list[dict]) -> list[TagGroup]:
    """Group tasks (and direct subtasks) by tag metadata."""
    tag_order: list[str] = []
    tag_seen: set[str] = set()
    buckets: dict[str, list[dict]] = {}

    def consider(task: dict) -> None:
        for tag in parse_tags(task.get("body")):
            if tag not in tag_seen:
                tag_seen.add(tag)
                tag_order.append(tag)
                buckets[tag] = []
            buckets[tag].append(task)

    for task in tasks:
        consider(task)
        for child in task.get("subtasks") or []:
            consider(child)

    groups: list[TagGroup] = []
    for tag in tag_order:
        rows = buckets[tag]
        open_rows: list[dict] = []
        done_rows: list[dict] = []
        for row in rows:
            entry = {
                "gid": row["gid"],
                "name": row.get("name", ""),
                "state": _task_state(row.get("body") or ""),
            }
            if row.get("completed"):
                done_entry = {
                    "gid": row["gid"],
                    "name": row.get("name", ""),
                    "completed_at": row.get("completed_at"),
                }
                done_rows.append(done_entry)
            else:
                open_rows.append(entry)

        finished = len(done_rows) >= 1 and len(open_rows) == 0
        finished_at: str | None = None
        if finished:
            times = [t for t in (r.get("completed_at") for r in done_rows) if t]
            if times:
                finished_at = max(times)

        groups.append(
            TagGroup(
                tag=tag,
                open=open_rows,
                done=done_rows,
                finished=finished,
                finished_at=finished_at,
            )
        )
    return groups
