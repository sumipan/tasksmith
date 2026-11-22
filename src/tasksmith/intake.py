"""Register tasks from a list of line items with shared tags."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from tasksmith.body import build_controlled_notes
from tasksmith.model import FLOW_DAG, FLOW_PERSONAL, OwnerType, PipelineState
from tasksmith.store import TaskStore, TaskStoreError
from tasksmith.tags import parse_tags, set_tags

__all__ = ["IntakeResult", "register_items"]


@dataclass
class IntakeResult:
    created: list[str]
    skipped: list[dict]
    failed: list[dict]


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


def register_items(
    items: list[dict],
    *,
    project_id: str | None,
    tags: Sequence[str],
    store: TaskStore,
    reference: str | None = None,
) -> IntakeResult:
    """Create tasks from intake items; skip duplicates under the same tag set."""
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

    for item in items:
        text = str(item.get("text", "")).strip()
        register = item.get("register", True)
        reason = item.get("reason") or "no reason"

        if not register:
            skipped.append({"text": text or str(item.get("text", "")), "reason": reason})
            continue

        if not text:
            skipped.append({"text": str(item.get("text", "")), "reason": "empty text"})
            continue

        raw_owner = item.get("owner")
        if raw_owner is None:
            owner = OwnerType.HUMAN
        elif raw_owner in (OwnerType.HUMAN.value, OwnerType.AI.value):
            owner = OwnerType(raw_owner)
        else:
            failed.append({"text": text, "error": f"invalid owner: {raw_owner}"})
            continue

        if (
            _already_registered(existing, tags=normalized, text=text)
            or text in seen_texts
        ):
            skipped.append({"text": text, "reason": "already registered"})
            seen_texts.add(text)
            continue

        flow = FLOW_PERSONAL if owner is OwnerType.HUMAN else FLOW_DAG
        body = build_controlled_notes(
            objective=text,
            owner=owner,
            state=PipelineState.REVIEW,
            flow=flow,
            context=reference or "",
        )
        body = set_tags(body, normalized)
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
            continue

        created.append(task_gid)
        seen_texts.add(text)
        existing.append(store.get_task(task_gid))

    return IntakeResult(created=created, skipped=skipped, failed=failed)
