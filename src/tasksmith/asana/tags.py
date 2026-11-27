"""Index and user tag sync from controlled body metadata."""

from __future__ import annotations

from typing import Any

from tasksmith.asana import api
from tasksmith.body import is_controlled_notes, parse_meta_field, parse_sections
from tasksmith.model import OwnerType, PipelineState
from tasksmith.tags import parse_tags

_STATE_OPTIONS = (
    PipelineState.READY,
    PipelineState.RUNNING,
    PipelineState.WAITING_HUMAN,
    PipelineState.REVIEW,
    PipelineState.DONE,
    PipelineState.BLOCKED,
)
_OWNER_OPTIONS = (OwnerType.AI, OwnerType.HUMAN, OwnerType.EXTERNAL)
_INDEX_NAMES: tuple[str, ...] = tuple(
    [f"state:{s.value}" for s in _STATE_OPTIONS]
    + [f"owner:{o.value}" for o in _OWNER_OPTIONS]
)


def _reserved_name(name: str) -> bool:
    return name.startswith("state:") or name.startswith("owner:")


def _body_index_tags(body: str) -> set[str]:
    sections = parse_sections(body)
    if sections is None:
        return set()
    names: set[str] = set()
    state = parse_meta_field(sections, "state")
    owner = parse_meta_field(sections, "owner")
    if state:
        names.add(f"state:{state}")
    if owner:
        names.add(f"owner:{owner}")
    return names


def _has_tags_line(body: str) -> bool:
    sections = parse_sections(body)
    if sections is None:
        return False
    return parse_meta_field(sections, "tags") is not None


def ensure_index_tag_gids(
    workspace_gid: str,
    *,
    client: Any,
    cache: dict[str, str],
) -> dict[str, str]:
    """Return name -> gid for index tags, using per-store cache."""
    if cache and all(name in cache for name in _INDEX_NAMES):
        return dict(cache)
    existing = {
        str(t["name"]): str(t["gid"])
        for t in api.list_workspace_tags(workspace_gid, client=client)
        if isinstance(t, dict) and t.get("name") and t.get("gid")
    }
    for name in _INDEX_NAMES:
        if name in existing:
            cache[name] = existing[name]
            continue
        created = api.create_tag(workspace_gid, name=name, client=client)
        cache[name] = str(created["gid"])
    return dict(cache)


def _ensure_user_tag_gid(
    workspace_gid: str,
    name: str,
    *,
    client: Any,
    cache: dict[str, dict[str, str]],
) -> str:
    by_ws = cache.setdefault(workspace_gid, {})
    if name in by_ws:
        return by_ws[name]
    existing = {
        str(t["name"]): str(t["gid"])
        for t in api.list_workspace_tags(workspace_gid, client=client)
        if isinstance(t, dict) and t.get("name") and t.get("gid")
    }
    by_ws.update(existing)
    if name in by_ws:
        return by_ws[name]
    created = api.create_tag(workspace_gid, name=name, client=client)
    gid = str(created["gid"])
    by_ws[name] = gid
    return gid


def sync_index_tags(
    task_gid: str,
    body: str,
    *,
    workspace_gid: str,
    client: Any,
    index_cache: dict[str, str],
) -> list[str]:
    """Sync state:/owner: tags. Returns action labels."""
    if not is_controlled_notes(body):
        return []
    desired = _body_index_tags(body)
    current = api.get_task(
        task_gid, opt_fields="tags.name,tags.gid", client=client
    )
    tags = current.get("tags") if isinstance(current.get("tags"), list) else []
    current_index = [
        t
        for t in tags
        if isinstance(t, dict)
        and isinstance(t.get("name"), str)
        and _reserved_name(str(t["name"]))
    ]
    current_names = {str(t["name"]) for t in current_index}
    if current_names == desired:
        return []

    gids = ensure_index_tag_gids(workspace_gid, client=client, cache=index_cache)
    actions: list[str] = []
    for tag in current_index:
        name = str(tag["name"])
        if name in desired:
            continue
        tag_gid = tag.get("gid") or gids.get(name)
        if tag_gid:
            api.remove_tag_from_task(task_gid, tag_gid=str(tag_gid), client=client)
            actions.append(f"removed {name}")
    for name in sorted(desired):
        if name in current_names:
            continue
        tag_gid = gids.get(name)
        if tag_gid:
            api.add_tag_to_task(task_gid, tag_gid=tag_gid, client=client)
            actions.append(f"added {name}")
    return actions


def sync_user_tags(
    task_gid: str,
    body: str,
    *,
    workspace_gid: str,
    client: Any,
    user_cache: dict[str, dict[str, str]],
) -> list[str]:
    """Sync user tags from control metadata. Returns action labels."""
    if not isinstance(body, str) or not is_controlled_notes(body):
        return []
    if not _has_tags_line(body):
        return []

    desired = {t for t in parse_tags(body) if not _reserved_name(t)}
    current = api.get_task(
        task_gid, opt_fields="tags.name,tags.gid", client=client
    )
    tags = current.get("tags") if isinstance(current.get("tags"), list) else []
    current_user = [
        t
        for t in tags
        if isinstance(t, dict)
        and isinstance(t.get("name"), str)
        and not _reserved_name(str(t["name"]))
    ]
    current_names = {str(t["name"]) for t in current_user}
    if current_names == desired:
        return []

    actions: list[str] = []
    for tag in current_user:
        name = str(tag["name"])
        if name in desired:
            continue
        tag_gid = tag.get("gid") or user_cache.get(workspace_gid, {}).get(name)
        if tag_gid:
            api.remove_tag_from_task(task_gid, tag_gid=str(tag_gid), client=client)
            actions.append(f"removed {name}")

    for name in sorted(desired):
        if name in current_names:
            continue
        tag_gid = _ensure_user_tag_gid(
            workspace_gid, name, client=client, cache=user_cache
        )
        api.add_tag_to_task(task_gid, tag_gid=tag_gid, client=client)
        actions.append(f"added {name}")
    return actions
