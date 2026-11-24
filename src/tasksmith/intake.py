"""Register tasks from a list of line items with shared tags."""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from tasksmith.body import build_controlled_notes
from tasksmith.model import FLOW_DAG, FLOW_PERSONAL, OwnerType, PipelineState
from tasksmith.settings import settings
from tasksmith.store import TaskStore, TaskStoreError
from tasksmith.tags import parse_tags, set_tags

__all__ = ["IntakeResult", "mark_seen", "register_items", "seen_ids", "unseen"]

_VALID_OUTCOMES = frozenset({"created", "duplicate", "not_registered"})


@dataclass
class IntakeResult:
    created: list[str]
    skipped: list[dict]
    failed: list[dict]


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_seen_path(path: Path | None) -> Path:
    if path is not None:
        return path
    return settings().state_dir / "intake-seen.jsonl"


def _lock_path(seen_path: Path) -> Path:
    return Path(f"{seen_path}.lock")


@contextmanager
def _seen_lock(seen_path: Path) -> Iterator[None]:
    lock = _lock_path(seen_path)
    seen_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 30.0
    fd = -1
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.monotonic() > deadline:
                raise TimeoutError(f"timed out waiting for intake lock at {lock}")
            time.sleep(0.01)
    try:
        yield
    finally:
        if fd >= 0:
            os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            lock.unlink()


def _parse_record_line(line: str, line_no: int) -> dict:
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(f"intake-seen.jsonl line {line_no}: invalid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"intake-seen.jsonl line {line_no}: expected JSON object")
    for key in ("source_id", "outcome", "task_gid", "at"):
        if key not in raw:
            raise ValueError(f"intake-seen.jsonl line {line_no}: missing {key!r}")
    outcome = raw["outcome"]
    if outcome not in _VALID_OUTCOMES:
        raise ValueError(f"intake-seen.jsonl line {line_no}: unknown outcome {outcome!r}")
    source_id = raw["source_id"]
    if not isinstance(source_id, str) or not source_id:
        raise ValueError(f"intake-seen.jsonl line {line_no}: invalid source_id")
    return raw


def _load_seen_ids(seen_path: Path) -> set[str]:
    if not seen_path.exists():
        return set()
    ids: set[str] = set()
    with seen_path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            stripped = line.strip()
            if not stripped:
                continue
            record = _parse_record_line(stripped, line_no)
            ids.add(record["source_id"])
    return ids


def _append_record(seen_path: Path, *, source_id: str, outcome: str, task_gid: str | None) -> None:
    record = {
        "source_id": source_id,
        "outcome": outcome,
        "task_gid": task_gid,
        "at": _utc_iso(),
    }
    seen_path.parent.mkdir(parents=True, exist_ok=True)
    with seen_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()


def seen_ids(path: Path | None = None) -> set[str]:
    """Return all source IDs recorded in the intake seen ledger."""
    return _load_seen_ids(_resolve_seen_path(path))


def unseen(source_ids: Sequence[str], path: Path | None = None) -> list[str]:
    """Return source IDs from the input that are not yet recorded, in input order."""
    known = seen_ids(path)
    out: list[str] = []
    seen_input: set[str] = set()
    for raw in source_ids:
        if raw in seen_input or raw in known:
            continue
        seen_input.add(raw)
        out.append(raw)
    return out


def mark_seen(
    source_ids: Sequence[str],
    *,
    outcome: str = "not_registered",
    path: Path | None = None,
) -> None:
    """Append ledger records for source IDs that are not yet recorded."""
    if outcome not in _VALID_OUTCOMES:
        raise ValueError(f"unknown outcome: {outcome!r}")
    seen_path = _resolve_seen_path(path)
    with _seen_lock(seen_path):
        known = _load_seen_ids(seen_path)
        for source_id in source_ids:
            if source_id in known:
                continue
            _append_record(seen_path, source_id=source_id, outcome=outcome, task_gid=None)
            known.add(source_id)


def _source_id_value(item: dict) -> str | None | bool:
    if "source_id" not in item:
        return None
    raw = item.get("source_id")
    if raw is None:
        return None
    if not isinstance(raw, str):
        return False  # type: ignore[return-value]
    text = raw.strip()
    if not text:
        return None
    return text


def _already_registered(
    existing: list[dict],
    *,
    tags: Sequence[str],
    text: str,
) -> bool:
    tag_set = set(tags)
    for task in existing:
        if task.get("name") != text:
            continue
        if tag_set.intersection(parse_tags(task.get("body"))):
            return True
    return False


def _process_plain_item(
    item: dict,
    *,
    normalized_tags: Sequence[str],
    existing: list[dict],
    seen_texts: set[str],
    store: TaskStore,
    project_id: str | None,
    reference: str | None,
    created: list[str],
    skipped: list[dict],
    failed: list[dict],
) -> None:
    text = str(item.get("text", "")).strip()
    register = item.get("register", True)
    reason = item.get("reason") or "no reason"

    if not register:
        skipped.append({"text": text or str(item.get("text", "")), "reason": reason})
        return

    if not text:
        skipped.append({"text": str(item.get("text", "")), "reason": "empty text"})
        return

    raw_owner = item.get("owner")
    if raw_owner is None:
        owner = OwnerType.HUMAN
    elif raw_owner in (OwnerType.HUMAN.value, OwnerType.AI.value):
        owner = OwnerType(raw_owner)
    else:
        failed.append({"text": text, "error": f"invalid owner: {raw_owner}"})
        return

    if _already_registered(existing, tags=normalized_tags, text=text) or text in seen_texts:
        skipped.append({"text": text, "reason": "already registered"})
        seen_texts.add(text)
        return

    flow = FLOW_PERSONAL if owner is OwnerType.HUMAN else FLOW_DAG
    body = build_controlled_notes(
        objective=text,
        owner=owner,
        state=PipelineState.REVIEW,
        flow=flow,
        context=reference or "",
    )
    body = set_tags(body, normalized_tags)
    section = item.get("section")
    section_gid = section if isinstance(section, str) and section else None

    try:
        task_gid = store.create_task(
            text,
            project_gid=project_id,
            section_gid=section_gid,
            body=body,
        )
    except TaskStoreError as exc:
        failed.append({"text": text, "error": str(exc)})
        return

    created.append(task_gid)
    seen_texts.add(text)
    existing.append(store.get_task(task_gid))


def _process_seen_item(
    item: dict,
    *,
    source_id: str,
    seen_path: Path,
    seen_in_call: set[str],
    normalized_tags: Sequence[str],
    existing: list[dict],
    store: TaskStore,
    project_id: str | None,
    reference: str | None,
    created: list[str],
    skipped: list[dict],
    failed: list[dict],
) -> None:
    text = str(item.get("text", "")).strip()
    register = item.get("register", True)
    reason = item.get("reason") or "no reason"

    with _seen_lock(seen_path):
        known = _load_seen_ids(seen_path)
        if source_id in known or source_id in seen_in_call:
            skipped.append({"text": text or str(item.get("text", "")), "reason": "already processed"})
            seen_in_call.add(source_id)
            return

        if not register:
            skipped.append({"text": text or str(item.get("text", "")), "reason": reason})
            _append_record(
                seen_path,
                source_id=source_id,
                outcome="not_registered",
                task_gid=None,
            )
            seen_in_call.add(source_id)
            return

        if not text:
            skipped.append({"text": str(item.get("text", "")), "reason": "empty text"})
            _append_record(
                seen_path,
                source_id=source_id,
                outcome="not_registered",
                task_gid=None,
            )
            seen_in_call.add(source_id)
            return

        raw_owner = item.get("owner")
        if raw_owner is None:
            owner = OwnerType.HUMAN
        elif raw_owner in (OwnerType.HUMAN.value, OwnerType.AI.value):
            owner = OwnerType(raw_owner)
        else:
            failed.append({"text": text, "error": f"invalid owner: {raw_owner}"})
            return

        if _already_registered(existing, tags=normalized_tags, text=text):
            skipped.append({"text": text, "reason": "already registered"})
            _append_record(
                seen_path,
                source_id=source_id,
                outcome="duplicate",
                task_gid=None,
            )
            seen_in_call.add(source_id)
            return

        flow = FLOW_PERSONAL if owner is OwnerType.HUMAN else FLOW_DAG
        body = build_controlled_notes(
            objective=text,
            owner=owner,
            state=PipelineState.REVIEW,
            flow=flow,
            context=reference or "",
        )
        body = set_tags(body, normalized_tags)
        section = item.get("section")
        section_gid = section if isinstance(section, str) and section else None

        try:
            task_gid = store.create_task(
                text,
                project_gid=project_id,
                section_gid=section_gid,
                body=body,
            )
        except TaskStoreError as exc:
            failed.append({"text": text, "error": str(exc)})
            return

        _append_record(
            seen_path,
            source_id=source_id,
            outcome="created",
            task_gid=task_gid,
        )
        created.append(task_gid)
        seen_in_call.add(source_id)
        existing.append(store.get_task(task_gid))


def register_items(
    items: list[dict],
    *,
    project_id: str | None,
    tags: Sequence[str],
    store: TaskStore,
    reference: str | None = None,
    seen_path: Path | None = None,
    use_seen: bool = True,
) -> IntakeResult:
    """Create tasks from intake items; skip duplicates under the same tag set.

    Items may carry an optional ``source_id`` (non-empty string). When ``use_seen``
    is true, each distinct ``source_id`` is processed at most once per ledger; callers
    that emit multiple tasks from one origin record should suffix IDs (``<id>#1``,
    ``<id>#2``) and call ``mark_seen([origin_id])`` after all sibling items finish.
    """
    if not tags:
        raise ValueError("at least one tag is required")
    tagged = set_tags(
        build_controlled_notes(objective="x", state=PipelineState.REVIEW),
        tags,
    )
    normalized = parse_tags(tagged)

    existing = store.list_tasks(project_id)
    created: list[str] = []
    skipped: list[dict] = []
    failed: list[dict] = []
    seen_texts: set[str] = set()
    seen_in_call: set[str] = set()
    resolved_seen: Path | None = None

    for item in items:
        sid = _source_id_value(item)
        if sid is False:
            text = str(item.get("text", "")).strip() or str(item.get("text", ""))
            failed.append({"text": text, "error": "invalid source_id"})
            continue

        if use_seen and sid is not None:
            if resolved_seen is None:
                resolved_seen = _resolve_seen_path(seen_path)
            _process_seen_item(
                item,
                source_id=sid,
                seen_path=resolved_seen,
                seen_in_call=seen_in_call,
                normalized_tags=normalized,
                existing=existing,
                store=store,
                project_id=project_id,
                reference=reference,
                created=created,
                skipped=skipped,
                failed=failed,
            )
            continue

        _process_plain_item(
            item,
            normalized_tags=normalized,
            existing=existing,
            seen_texts=seen_texts,
            store=store,
            project_id=project_id,
            reference=reference,
            created=created,
            skipped=skipped,
            failed=failed,
        )

    return IntakeResult(created=created, skipped=skipped, failed=failed)
