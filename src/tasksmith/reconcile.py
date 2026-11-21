"""Reconcile: classify incomplete tasks without writes."""

from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from tasksmith.body import (
    FORMAT_VERSION,
    format_version,
    is_controlled_notes,
    parse_meta_field,
    parse_sections,
    parse_start_on,
)
from tasksmith.language import language
from tasksmith.model import OwnerType, PipelineState
from tasksmith.runtime import task_store
from tasksmith.settings import settings

__all__ = [
    "ReconcileNode",
    "ReconcileResult",
    "reconcile",
    "_SECTION_UNRESOLVED_KEY",
    "_body_meta",
    "_flatten_incomplete_tasks",
    "_load_sections_config",
    "_project_gid",
]

# Body states for which completed=true may adopt DONE as the state (RUNNING is excluded because the running order writes it)
_ADOPTABLE_COMPLETION_STATES = frozenset(
    {
        PipelineState.READY.value,
        PipelineState.REVIEW.value,
        PipelineState.WAITING_HUMAN.value,
        PipelineState.BLOCKED.value,
    }
)

_SECTION_UNRESOLVED_KEY = "_section_unresolved"

_ACTOR_RE = re.compile(r"^ACTOR:\s*(.+)$", re.M)
_NOTES_CHANGE_RE = re.compile(r"changed the description|changed the name of this task", re.I)


@dataclass(frozen=True)
class ReconcileNode:
    task_gid: str
    name: str
    category: str  # ready | review | blocked | waiting_human | overdue | drift | unmanaged
    reason: str
    due_on: str | None
    owner_type: str | None
    pipeline_state: str | None
    permalink_url: str | None
    flow_type: str | None = None  # "dag" | "personal" | None (None treated as personal)
    # Parent gid when this is a subtask (used for unsectioned / section_unresolved)
    parent_gid: str | None = None
    # Parent: a non-default lane id in the store. Subtask: the parent body's section:, or else a non-default lane id of the parent
    section_hint: str | None = None
    # Body captured by reconcile (for tick's design_doc_gate; not emitted by result_to_dict)
    body: str | None = None
    location: str | None = None


@dataclass(frozen=True)
class ReconcileResult:
    ready: list[ReconcileNode]
    waiting_human: list[ReconcileNode]
    blocked: list[ReconcileNode]
    drift: list[ReconcileNode]
    overdue: list[ReconcileNode]
    unmanaged: list[ReconcileNode]
    elapsed_seconds: float
    review: list[ReconcileNode] = field(default_factory=list)
    completable_parents: list[ReconcileNode] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sortable_parent_gids: list[str] = field(default_factory=list)
    unsectioned: list[ReconcileNode] = field(default_factory=list)
    # Managed tasks whose body has _section_unresolved (assign_sections does not retry them)
    section_unresolved: list[ReconcileNode] = field(default_factory=list)
    # gid of unsectioned -> body at reconcile time (for the assign_sections dry-run decision; not emitted in JSON)
    section_notes: dict[str, str] = field(default_factory=dict, repr=False)
    # Managed nodes a human set to completed / not completed in the store (adopt_completions adopts them into state)
    adoptable_completions: list[ReconcileNode] = field(default_factory=list)
    adoptable_reopens: list[ReconcileNode] = field(default_factory=list)
    inverted_periods: list[ReconcileNode] = field(default_factory=list)


def _project_gid(project_gid: str | None) -> str | None:
    return project_gid or settings().project_id




def _body_meta(body: str | None) -> tuple[str | None, str | None]:
    """Read (state, owner) from the body. (None, None) when not in the fixed format."""
    if not isinstance(body, str) or not is_controlled_notes(body):
        return None, None
    sections = parse_sections(body)
    if sections is None:
        return None, None
    return parse_meta_field(sections, "state"), parse_meta_field(sections, "owner")


def _body_depends_on(body: str | None) -> list[str]:
    """Read the GID list from ``depends_on:`` of the body control metadata (source of truth for dependencies)."""
    if not isinstance(body, str) or not is_controlled_notes(body):
        return []
    sections = parse_sections(body)
    if sections is None:
        return []
    raw = parse_meta_field(sections, "depends_on")
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _index_all_tasks(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index parents and subtasks by GID, including completed ones (for dependency satisfaction checks)."""
    by_gid: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        gid = str(row.get("gid") or "")
        if gid:
            by_gid[gid] = row
        for sub in row.get("subtasks") or []:
            if not isinstance(sub, dict):
                continue
            sgid = str(sub.get("gid") or "")
            if sgid:
                by_gid[sgid] = sub
    return by_gid


def _is_dependency_done(dep_gid: str, all_by_gid: dict[str, dict[str, Any]]) -> bool:
    """A dependency is satisfied when it is completed or its body state is DONE."""
    task = all_by_gid.get(dep_gid)
    if task is None:
        return False
    if task.get("completed"):
        return True
    notes = task.get("body")
    state, _ = _body_meta(notes if isinstance(notes, str) else None)
    return state == PipelineState.DONE.value


def _flatten_incomplete_tasks(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        gid = str(row.get("gid") or "")
        if gid and not row.get("completed") and gid not in seen:
            seen.add(gid)
            out.append(row)
        for sub in row.get("subtasks") or []:
            if not isinstance(sub, dict):
                continue
            sgid = str(sub.get("gid") or "")
            if sgid and not sub.get("completed") and sgid not in seen:
                seen.add(sgid)
                out.append(sub)
    return out


def _find_cycles(deps_map: dict[str, list[str]]) -> set[str]:
    """Detect nodes on a cycle from dependency edges task -> dependency (task waits for dependency) (Kahn)."""
    nodes = set(deps_map)
    for deps in deps_map.values():
        nodes.update(deps)
    # edge: dependency -> task (the dependency completes first)
    successors: dict[str, list[str]] = defaultdict(list)
    in_degree: dict[str, int] = {n: 0 for n in nodes}
    for task_gid, deps in deps_map.items():
        for dep in deps:
            successors[dep].append(task_gid)
            in_degree[task_gid] = in_degree.get(task_gid, 0) + 1
            in_degree.setdefault(dep, 0)
    q: deque[str] = deque([n for n, d in in_degree.items() if d == 0])
    seen = 0
    while q:
        n = q.popleft()
        seen += 1
        for nxt in successors.get(n, []):
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                q.append(nxt)
    if seen == len(in_degree):
        return set()
    return {n for n, d in in_degree.items() if d > 0}


def _latest_actor(stories: list[dict[str, Any]]) -> str | None:
    for story in reversed(stories):
        text = story.get("text") or ""
        if "PIPELINE_STATUS:" not in text:
            continue
        m = _ACTOR_RE.search(text)
        if m:
            return m.group(1).strip()
    return None


def _waiting_since(stories: list[dict[str, Any]]) -> str | None:
    for story in reversed(stories):
        text = story.get("text") or ""
        if "PIPELINE_STATUS: WAITING_HUMAN" in text:
            return story.get("created_at")
    return None


def _detect_notes_drift(
    stories: list[dict[str, Any]], *, ai_names: frozenset[str]
) -> str | None:
    """Detect body updates by non-secretary accounts (tag drift is unified into the body-vs-tag comparison)."""
    recent = stories[-20:] if len(stories) > 20 else stories
    ordered = sorted(recent, key=lambda s: s.get("created_at") or "", reverse=True)

    for story in ordered:
        text = story.get("text") or ""
        created_by = (
            (story.get("created_by") or {}) if isinstance(story.get("created_by"), dict) else {}
        )
        by_name = created_by.get("name") or ""
        if _NOTES_CHANGE_RE.search(text):
            if by_name not in ai_names:
                return language().messages["drift.notes_changed_by_non_ai"].format(name=by_name or 'unknown')
    return None


def _load_sections_config() -> Any:
    return settings().sections


def _empty_reconcile(elapsed: float) -> ReconcileResult:
    return ReconcileResult(
        ready=[],
        waiting_human=[],
        blocked=[],
        drift=[],
        overdue=[],
        unmanaged=[],
        elapsed_seconds=elapsed,
        review=[],
        completable_parents=[],
        warnings=[],
        unsectioned=[],
        section_unresolved=[],
    )


def _find_completable_parents(rows: list[dict[str, Any]]) -> list[ReconcileNode]:
    """Return RUNNING parents whose direct children are all state=DONE and completed=true."""
    out: list[ReconcileNode] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("completed"):
            continue
        parent_gid = str(row.get("gid") or "")
        if not parent_gid:
            continue
        subs = [s for s in (row.get("subtasks") or []) if isinstance(s, dict) and s.get("gid")]
        if not subs:
            continue
        parent_notes = row.get("body") if isinstance(row.get("body"), str) else None
        if not parent_notes or not is_controlled_notes(parent_notes):
            continue
        if format_version(parent_notes) != FORMAT_VERSION:
            continue
        p_state, p_owner = _body_meta(parent_notes)
        if p_state != PipelineState.RUNNING.value:
            continue

        ok = True
        for sub in subs:
            sub_notes = sub.get("body") if isinstance(sub.get("body"), str) else None
            if not sub_notes or not is_controlled_notes(sub_notes):
                ok = False
                break
            if format_version(sub_notes) != FORMAT_VERSION:
                ok = False
                break
            s_state, _ = _body_meta(sub_notes)
            completed = bool(sub.get("completed"))
            if s_state != PipelineState.DONE.value or not completed:
                ok = False
                break
        if not ok:
            continue
        p_secs = parse_sections(parent_notes)
        p_location = parse_meta_field(p_secs, "location") if p_secs else None
        out.append(
            ReconcileNode(
                task_gid=parent_gid,
                name=str(row.get("name") or ""),
                category="completable_parent",
                reason=language().messages["reconcile.completable_parent"].format(count=len(subs)),
                due_on=str(row["due_on"]) if row.get("due_on") else None,
                owner_type=p_owner,
                pipeline_state=p_state,
                permalink_url=str(row["permalink_url"]) if row.get("permalink_url") else None,
                location=p_location,
            )
        )
    return out


def reconcile(
    project_gid: str | None = None,
) -> ReconcileResult:
    """Classify incomplete tasks (no writes). State, owner and dependencies are taken from the body."""
    started = time.perf_counter()
    pid = _project_gid(project_gid)
    today = date.today().isoformat()

    try:
        rows = task_store().list_tasks(pid)
        incomplete = _flatten_incomplete_tasks(rows)
        by_gid = {str(t["gid"]): t for t in incomplete if t.get("gid")}
        all_by_gid = _index_all_tasks(rows)

        # Resolve the secretary account name via /users/me with the secretary token (do not use the user's base client)
        ai_names = task_store().ai_actor_names()
        warnings: list[str] = []
        if not ai_names:
            warnings.append(language().messages["reconcile.warning.secretary_token_missing"])

        # The source of truth for dependencies is body depends_on: (the paid dependency API is not used)
        deps_map: dict[str, list[str]] = {}
        for gid, task in by_gid.items():
            notes_s = task.get("body") if isinstance(task.get("body"), str) else None
            deps_map[gid] = _body_depends_on(notes_s)

        cycle_nodes = _find_cycles(deps_map)
        cycle_list = sorted(cycle_nodes)

        state_by_gid: dict[str, str | None] = {}
        owner_by_gid: dict[str, str | None] = {}
        flow_by_gid: dict[str, str | None] = {}
        location_by_gid: dict[str, str | None] = {}
        controlled_by_gid: dict[str, bool] = {}
        for gid, task in by_gid.items():
            body = task.get("body")
            notes_s = body if isinstance(body, str) else None
            controlled_by_gid[gid] = is_controlled_notes(notes_s)
            state, owner = _body_meta(notes_s)
            state_by_gid[gid] = state
            owner_by_gid[gid] = owner
            if notes_s:
                secs_for_flow = parse_sections(notes_s)
                flow_by_gid[gid] = parse_meta_field(secs_for_flow, "flow") if secs_for_flow else None
                location_by_gid[gid] = (
                    parse_meta_field(secs_for_flow, "location") if secs_for_flow else None
                )
            else:
                flow_by_gid[gid] = None
                location_by_gid[gid] = None

        categorized: dict[str, list[ReconcileNode]] = {
            "ready": [],
            "review": [],
            "waiting_human": [],
            "blocked": [],
            "drift": [],
            "overdue": [],
            "unmanaged": [],
            "unsectioned": [],
            "section_unresolved": [],
            "adoptable_completions": [],
            "adoptable_reopens": [],
        }

        for gid, task in by_gid.items():
            name = str(task.get("name") or "")
            due_on = task.get("due_on")
            due_s = str(due_on) if due_on else None
            permalink = task.get("permalink_url")
            permalink_s = str(permalink) if permalink else None
            owner = owner_by_gid.get(gid)
            state = state_by_gid.get(gid)
            flow = flow_by_gid.get(gid)
            controlled = controlled_by_gid.get(gid, False)
            notes_s = task.get("body") if isinstance(task.get("body"), str) else None

            def node(category: str, reason: str) -> ReconcileNode:
                return ReconcileNode(
                    task_gid=gid,
                    name=name,
                    category=category,
                    reason=reason,
                    due_on=due_s,
                    owner_type=owner,
                    pipeline_state=state,
                    permalink_url=permalink_s,
                    flow_type=flow,
                    body=notes_s,
                    location=location_by_gid.get(gid),
                )

            # 1. blocked
            if gid in cycle_nodes:
                categorized["blocked"].append(
                    node("blocked", language().messages["reconcile.dependency_cycle"].format(cycle=', '.join(cycle_list)))
                )
                continue
            if state == PipelineState.BLOCKED.value:
                categorized["blocked"].append(node("blocked", "PipelineState=BLOCKED"))
                continue
            dep_block_reason = None
            for dep_gid in deps_map.get(gid, []):
                if not _is_dependency_done(dep_gid, all_by_gid):
                    dep_block_reason = language().messages["reconcile.dependency_pending"].format(dep_gid=dep_gid)
                    break
            if dep_block_reason:
                categorized["blocked"].append(node("blocked", dep_block_reason))
                continue

            # Unmanaged (state:/owner: missing or not in the fixed format) comes before drift / ready
            if not controlled or owner is None or state is None:
                if not controlled and (owner is None or state is None):
                    reason = language().messages["reconcile.unmanaged.not_fixed_and_unset"]
                elif not controlled:
                    reason = language().messages["reconcile.unmanaged.not_fixed_format"]
                else:
                    reason = language().messages["reconcile.unmanaged.unset"]
                categorized["unmanaged"].append(node("unmanaged", reason))
                # Even if unmanaged, an overdue task also goes into overdue (duplicates allowed)
                if due_s and due_s < today and not task.get("completed"):
                    categorized["overdue"].append(
                        node("overdue", language().messages["reconcile.overdue_unmanaged"].format(due_s=due_s))
                    )
                continue

            # state: DONE but not completed in the store -> adoption candidate for the revert to incomplete (do not drop into overdue)
            if state == PipelineState.DONE.value and not task.get("completed"):
                categorized["adoptable_reopens"].append(
                    node("adoptable_reopen", "completed=false / state=DONE")
                )
                continue

            # 2. ready (AI / HUMAN; EXTERNAL is out of scope for Phase 2)
            # dependencies are already satisfied above through the body depends_on
            if state == PipelineState.READY.value and owner in (
                OwnerType.AI.value,
                OwnerType.HUMAN.value,
            ):
                categorized["ready"].append(
                    node(
                        "ready",
                        language().messages["reconcile.ready"].format(owner=owner),
                    )
                )
                continue

            # 2b. review (managed, intent stage; before the gate)
            if state == PipelineState.REVIEW.value and owner in (
                OwnerType.AI.value,
                OwnerType.HUMAN.value,
            ):
                categorized["review"].append(
                    node(
                        "review",
                        language().messages["reconcile.review"].format(owner=owner),
                    )
                )
                continue

            # 3. waiting_human
            if state == PipelineState.WAITING_HUMAN.value and owner == OwnerType.HUMAN.value:
                stories = task_store().get_task_stories(gid)
                secs = parse_sections(notes_s) if notes_s else None
                since = (
                    parse_meta_field(secs, "_waiting_since") if secs else None
                ) or _waiting_since(stories)
                reason = language().messages["reconcile.waiting_human"]
                if since:
                    reason += language().messages["reconcile.waiting_since"].format(since=since)
                categorized["waiting_human"].append(node("waiting_human", reason))
                continue

            # 4. overdue
            if due_s and due_s < today and not task.get("completed"):
                categorized["overdue"].append(node("overdue", language().messages["reconcile.overdue"].format(due_s=due_s)))
                continue

            # 5. drift (only managed nodes get their body changes checked via stories; skipped when the secretary name cannot be resolved)
            if ai_names:
                stories = task_store().get_task_stories(gid)
                drift_reason = _detect_notes_drift(stories, ai_names=ai_names)
                if drift_reason:
                    categorized["drift"].append(node("drift", drift_reason))
                    continue

            # Anything else (e.g. RUNNING and within due date, no drift) is out of classification = skipped

        # completed=true and body state is not DONE (the main loop handles incomplete tasks only)
        # -> adoption candidate for completion. RUNNING etc. stay as drift because the running order writes the body
        for gid, task in all_by_gid.items():
            if not task.get("completed"):
                continue
            notes_s = task.get("body") if isinstance(task.get("body"), str) else None
            if not notes_s or not is_controlled_notes(notes_s):
                continue
            state, owner = _body_meta(notes_s)
            if state is None or state == PipelineState.DONE.value:
                continue
            secs_c = parse_sections(notes_s)
            flow_c = parse_meta_field(secs_c, "flow") if secs_c else None
            if state in _ADOPTABLE_COMPLETION_STATES:
                category = "adoptable_completion"
                bucket = "adoptable_completions"
                reason = f"completed=true / state={state}"
            else:
                category = "drift"
                bucket = "drift"
                reason = f"state_completed_mismatch: completed=true / state={state}"
            categorized[bucket].append(
                ReconcileNode(
                    task_gid=gid,
                    name=str(task.get("name") or ""),
                    category=category,
                    reason=reason,
                    due_on=str(task["due_on"]) if task.get("due_on") else None,
                    owner_type=owner,
                    pipeline_state=state,
                    permalink_url=(
                        str(task["permalink_url"]) if task.get("permalink_url") else None
                    ),
                    flow_type=flow_c,
                    location=location_by_gid.get(gid),
                )
            )

        categorized["ready"].sort(
            key=lambda n: (n.due_on is None, n.due_on or "", n.name or "", n.task_gid)
        )

        completable = _find_completable_parents(rows)

        # Parent GIDs that are managed and have direct children (kept in current order)
        sortable_parent_gids: list[str] = []
        for row in rows:
            if not isinstance(row, dict) or row.get("completed"):
                continue
            gid_r = str(row.get("gid") or "")
            if not gid_r:
                continue
            subs = [s for s in (row.get("subtasks") or []) if isinstance(s, dict) and s.get("gid")]
            if not subs:
                continue
            if not controlled_by_gid.get(gid_r, False):
                continue
            sortable_parent_gids.append(gid_r)
        # unsectioned: managed tasks whose body has no section:, or that are in the default section in the store
        # Tasks whose body has _section_unresolved go to section_unresolved (assign_sections does not retry them)
        section_notes: dict[str, str] = {}
        sections_cfg = _load_sections_config()
        if sections_cfg is not None:
            default_def = sections_cfg.default_section()
            default_gid = default_def.gid if default_def else None
            pid = _project_gid(project_gid)
            lane_id_by_gid = {s.gid: s.id for s in sections_cfg.sections if not s.default}
            parent_of: dict[str, str] = {}
            for row in rows:
                if not isinstance(row, dict) or not row.get("gid"):
                    continue
                for sub in row.get("subtasks") or []:
                    if isinstance(sub, dict) and sub.get("gid"):
                        parent_of[str(sub["gid"])] = str(row["gid"])

            def _lane_id(task: dict[str, Any]) -> str | None:
                sec_gid = task.get("section_gid") or None
                return lane_id_by_gid.get(sec_gid) if sec_gid else None

            for gid, task in by_gid.items():
                if not controlled_by_gid.get(gid, False):
                    continue
                notes_s = task.get("body") if isinstance(task.get("body"), str) else None
                secs = parse_sections(notes_s) if notes_s else None
                section_id = parse_meta_field(secs, "section") if secs else None
                current_sec_gid = task.get("section_gid") or None

                if not section_id:
                    reason = language().messages["reconcile.unsectioned_no_body"]
                elif default_gid and current_sec_gid == default_gid:
                    reason = language().messages["reconcile.section_default_unsynced"].format(section_id=section_id)
                else:
                    continue

                parent_gid = parent_of.get(gid)
                if parent_gid is None:
                    hint = _lane_id(task)
                else:
                    parent = all_by_gid.get(parent_gid) or {}
                    p_notes = parent.get("body") if isinstance(parent.get("body"), str) else None
                    p_secs = parse_sections(p_notes) if p_notes else None
                    p_section = parse_meta_field(p_secs, "section") if p_secs else None
                    hint = (sections_cfg.normalize_id(p_section) if p_section else None) or _lane_id(parent)

                unresolved = bool(secs and parse_meta_field(secs, _SECTION_UNRESOLVED_KEY))
                due_on_v = task.get("due_on")
                permalink = task.get("permalink_url")
                bucket = "section_unresolved" if unresolved else "unsectioned"
                categorized[bucket].append(ReconcileNode(
                    task_gid=gid, name=str(task.get("name") or ""), category=bucket,
                    reason=reason,
                    due_on=str(due_on_v) if due_on_v else None,
                    owner_type=owner_by_gid.get(gid), pipeline_state=state_by_gid.get(gid),
                    permalink_url=str(permalink) if permalink else None,
                    flow_type=flow_by_gid.get(gid),
                    parent_gid=parent_gid, section_hint=hint,
                    location=location_by_gid.get(gid),
                ))
                if not unresolved:
                    section_notes[gid] = notes_s or ""

        inverted_periods: list[ReconcileNode] = []
        for gid, task in by_gid.items():
            if not controlled_by_gid.get(gid, False):
                continue
            notes_s = task.get("body") if isinstance(task.get("body"), str) else None
            due_on = task.get("due_on")
            due_s = str(due_on) if due_on else None
            start_on = parse_start_on(notes_s)
            if not start_on or not due_s or start_on <= due_s:
                continue
            name = str(task.get("name") or "")
            permalink = task.get("permalink_url")
            inverted_periods.append(
                ReconcileNode(
                    task_gid=gid,
                    name=name,
                    category="inverted_period",
                    reason=f"start_on={start_on} > due_on={due_s}",
                    due_on=due_s,
                    owner_type=owner_by_gid.get(gid),
                    pipeline_state=state_by_gid.get(gid),
                    permalink_url=str(permalink) if permalink else None,
                    flow_type=flow_by_gid.get(gid),
                    body=notes_s,
                    location=location_by_gid.get(gid),
                )
            )

        elapsed = time.perf_counter() - started
        return ReconcileResult(
            ready=categorized["ready"],
            waiting_human=categorized["waiting_human"],
            blocked=categorized["blocked"],
            drift=categorized["drift"],
            overdue=categorized["overdue"],
            unmanaged=categorized["unmanaged"],
            elapsed_seconds=round(elapsed, 4),
            review=categorized["review"],
            completable_parents=completable,
            warnings=warnings,
            sortable_parent_gids=sortable_parent_gids,
            unsectioned=categorized["unsectioned"],
            section_unresolved=categorized["section_unresolved"],
            section_notes=section_notes,
            adoptable_completions=categorized["adoptable_completions"],
            adoptable_reopens=categorized["adoptable_reopens"],
            inverted_periods=inverted_periods,
        )
    except Exception:
        elapsed = time.perf_counter() - started
        return _empty_reconcile(round(elapsed, 4))

