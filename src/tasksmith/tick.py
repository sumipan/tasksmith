"""Tick: advance READY nodes, finish-node, recover, metrics."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from tasksmith.body import (
    is_controlled_notes,
    parse_meta_field,
    parse_sections,
    replace_editable_sections,
    section_items,
    update_meta_field,
)
from tasksmith.events import EventKind, EventSink, NullSink, TasksmithEvent, first_line, safe_emit, utc_now_iso
from tasksmith.execution import executor
from tasksmith.external import (
    format_check_at,
    in_window,
    next_window_start,
    parse_check_at,
    parse_check_params,
    resolve_probe,
)
from tasksmith.gates import check_design_doc, format_violations_comment
from tasksmith.language import language
from tasksmith.model import OwnerType, PipelineState
from tasksmith.reconcile import (
    _SECTION_UNRESOLVED_KEY,
    ReconcileNode,
    ReconcileResult,
    _body_meta,
    _flatten_incomplete_tasks,
    _load_sections_config,
    _project_gid,
    reconcile,
)
from tasksmith.runtime import task_store
from tasksmith.settings import settings
from tasksmith.transition import transition_task

__all__ = [
    "AdoptResult",
    "FinishNodeResult",
    "RecoverResult",
    "SectionAssignment",
    "TickResult",
    "adopt_completions",
    "aggregate_metrics",
    "assign_sections",
    "finish_node",
    "format_metrics_text",
    "recover",
    "tick",
]

_log = logging.getLogger(__name__)

_PIPELINE_STATUS_RE = re.compile(r"^PIPELINE_STATUS:\s*(DONE|BLOCKED)\s*$", re.M)
_NOTE_RE = re.compile(r"^NOTE:\s*(.+)$", re.M)
_AC_DONE_RE = re.compile(r"^AC_DONE:\s*([\d,\s]+)\s*$", re.M)
_SKILL_EXECUTOR_RE = re.compile(r"^skill:(.+)$")
# Control metadata that finish_node compares before and after reflecting the result (detects rewrites by the executor)
_CONTROL_META_KEYS = (
    "format",
    "state",
    "owner",
    "executor",
    "depends_on",
    "inputs",
    "outputs",
    "flow",
    "section",
    "start_on",
    "location",
)


def _default_metrics_path() -> Path:
    return settings().state_dir / "metrics.jsonl"


def _append_metrics(record: dict[str, Any], *, metrics_path: Path | None = None) -> None:
    """Append one tasksmith JSONL line. Store state is not rolled back on failure."""
    path = metrics_path if metrics_path is not None else _default_metrics_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        _log.exception("tasksmith metrics write failed path=%s", path)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _snapshot_control_meta(sections: dict[str, str]) -> dict[str, str | None]:
    return {key: parse_meta_field(sections, key) for key in _CONTROL_META_KEYS}


def _control_meta_changed(
    before: dict[str, str | None], after: dict[str, str | None]
) -> list[str]:
    return [key for key in _CONTROL_META_KEYS if before.get(key) != after.get(key)]



def _execution_id(prefix: str, gid: str) -> str:
    return f"{prefix}-{gid}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"


def _emit_event(
    sink: EventSink | None,
    kind: EventKind,
    *,
    task_gid: str,
    task_name: Any,
    permalink_url: Any,
    summary: str,
    execution_id: str,
    actor: str,
) -> None:
    """Send an action event to the sink (no-op when sink is None; emit failures are swallowed)."""
    if sink is None:
        return
    safe_emit(
        sink,
        TasksmithEvent(
            kind=kind,
            task_gid=task_gid,
            task_name=task_name if isinstance(task_name, str) else "",
            permalink_url=permalink_url if isinstance(permalink_url, str) else "",
            summary=summary,
            execution_id=execution_id,
            actor=actor,
            ts=utc_now_iso(),
        ),
    )


def _resolve_event_sink(*, dry_run: bool = False) -> EventSink:
    """CLI default sink (NullSink); host.build_event_sink for production."""
    return NullSink()


def _count_running_from_project(
    project_gid: str | None) -> int:
    """Count incomplete tasks whose body state=RUNNING."""
    pid = _project_gid(project_gid)
    rows = task_store().list_tasks(pid)
    n = 0
    for task in _flatten_incomplete_tasks(rows):
        body = task.get("body")
        notes_s = body if isinstance(body, str) else None
        state, _owner = _body_meta(notes_s)
        if state == PipelineState.RUNNING.value:
            n += 1
    return n


def _control_meta_digest(sections: dict[str, str]) -> str:
    snap = _snapshot_control_meta(sections)
    payload = {k: v for k, v in snap.items() if k != "state"}
    raw = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _next_generation(gid: str) -> int:
    gen = 0
    while True:
        key = f"tasksmith:run:{gid}:{gen}"
        if not executor().used(key):
            return gen
        gen += 1
        if gen > 10_000:
            raise RuntimeError(f"idempotency generation overflow for {gid}")


def _latest_used_generation(gid: str) -> int | None:
    gen = 0
    latest: int | None = None
    while gen <= 10_000:
        key = f"tasksmith:run:{gid}:{gen}"
        if executor().used(key):
            latest = gen
            gen += 1
        else:
            break
    return latest


def _resolve_skill_executor(
    sections: dict[str, str],
) -> tuple[str | None, str | None]:
    """Return (skill_name, error_reason). skill_name is set when executor is valid."""
    exec_raw = parse_meta_field(sections, "executor") or ""
    skill_m = _SKILL_EXECUTOR_RE.match(exec_raw)
    if not skill_m:
        return None, language().messages["executor.not_skill_format"].format(exec_raw=exec_raw)
    skill_name = skill_m.group(1).strip()
    if not settings().executor_exists(skill_name):
        return None, language().messages["executor.skill_missing"].format(skill_name=skill_name)
    return skill_name, None


def _block_failed_running_ai_nodes(
    project_gid: str | None,
    *,
    dry_run: bool,
    messages: list[str],
) -> None:
    try:
        ex = executor()
    except RuntimeError:
        return
    pid = _project_gid(project_gid)
    try:
        rows = task_store().list_tasks(pid)
    except Exception:
        _log.warning("[tasksmith] failed-job sweep: list_tasks failed", exc_info=True)
        return
    for task in _flatten_incomplete_tasks(rows):
        body = task.get("body")
        if not isinstance(body, str) or not is_controlled_notes(body):
            continue
        sections = parse_sections(body)
        if sections is None:
            continue
        if parse_meta_field(sections, "state") != PipelineState.RUNNING.value:
            continue
        if parse_meta_field(sections, "owner") != OwnerType.AI.value:
            continue
        flow = parse_meta_field(sections, "flow")
        if flow != "dag":
            continue
        gid = str(task.get("gid") or "")
        if not gid:
            continue
        gen = _latest_used_generation(gid)
        if gen is None:
            continue
        key = f"tasksmith:run:{gid}:{gen}"
        reason = ex.failure(key)
        if not reason:
            continue
        messages.append(f"failed_job gid={gid} → BLOCKED ({reason})")
        if dry_run:
            continue
        transition_task(
            gid,
            state=PipelineState.BLOCKED,
            actor="tasksmith-tick",
            execution_id=_execution_id("tick-fail", gid),
            detail=reason,
        )


@dataclass(frozen=True)
class TickResult:
    status: str  # enqueued | requested_human | reminded | noop | dry_run
    task_gid: str | None = None
    name: str | None = None
    order_path: str | None = None
    idempotency_key: str | None = None
    order_preview: str | None = None
    message: str = ""
    request_text: str | None = None
    reminded_gids: tuple[str, ...] = ()
    counts: dict[str, int] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


def _reconcile_counts(result: ReconcileResult) -> dict[str, int]:
    return {
        "ready": len(result.ready),
        "review": len(result.review),
        "waiting_human": len(result.waiting_human),
        "waiting_external": len(result.waiting_external),
        "blocked": len(result.blocked),
        "drift": len(result.drift),
        "overdue": len(result.overdue),
        "unmanaged": len(result.unmanaged),
        "completable_parents": len(result.completable_parents),
        "unsectioned": len(result.unsectioned),
        "section_unresolved": len(getattr(result, "section_unresolved", None) or []),
        "adoptable_completions": len(result.adoptable_completions),
        "adoptable_reopens": len(result.adoptable_reopens),
        "inverted_periods": len(getattr(result, "inverted_periods", None) or []),
    }


def _tz() -> ZoneInfo:
    return ZoneInfo(settings().timezone)


_QUIET_HOURS_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _parse_hhmm(s: str) -> tuple[int, int]:
    m = _QUIET_HOURS_TIME_RE.match(s.strip())
    if not m:
        raise ValueError(f"Not in HH:MM format: {s!r}")
    return int(m.group(1)), int(m.group(2))


def _is_quiet_hours(now: datetime, quiet_hours: list[str]) -> bool:
    """Whether the current time is within quiet_hours (start, end), including midnight wrap."""
    if not quiet_hours or len(quiet_hours) != 2:
        return False
    try:
        sh, sm = _parse_hhmm(quiet_hours[0])
        eh, em = _parse_hhmm(quiet_hours[1])
    except ValueError:
        return False
    now_jst = now.astimezone(_tz())
    now_min = now_jst.hour * 60 + now_jst.minute
    start_min = sh * 60 + sm
    end_min = eh * 60 + em
    if start_min <= end_min:
        return start_min <= now_min < end_min
    return now_min >= start_min or now_min < end_min


def _objective_text(sections: dict[str, str]) -> str:
    return sections.get("objective") or ""

def _design_doc_gate(
    task_gid: str,
    *,
    body: str | None = None,
    actor: str,
    dry_run: bool = False,
) -> list[Any]:
    """Deterministic gate when selecting READY. With violations, move to REVIEW and return the violations.

    A body of an old version is aligned to ``tasksmith/v1`` by the store when it
    reads it, so it is not upgraded here; with violations it falls to REVIEW.

    If ``body`` is a str it is evaluated as the body already fetched by reconcile and
    ``get_task`` is not called. If None, it is fetched with ``get_task`` as before.
    """
    if body is None:
        task = task_store().get_task(task_gid)
        body = task.get("body")
    if not isinstance(body, str):
        return []
    sections = parse_sections(body)
    if sections is None:
        return []
    violations = check_design_doc(sections, body=body)
    if not violations:
        return []

    listing = format_violations_comment(violations)
    detail = language().messages["control_plane.gate_violation"].format(listing=listing)
    if not dry_run:
        transition_task(
            task_gid,
            state=PipelineState.REVIEW,
            actor=actor,
            execution_id=_execution_id("design-gate", task_gid),
            detail=detail,
        )
    return list(violations)


def _meta_int(sections: dict[str, str], key: str, *, default: int = 0) -> int:
    raw = parse_meta_field(sections, key)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        return default


def _notify_external(name: str, text: str, report_to: str | None, messages: list[str]) -> None:
    from tasksmith.runtime import human_channel

    try:
        ch = human_channel()
        if report_to:
            ch.follow_up(report_to, text)
        else:
            ch.announce(text)
    except Exception as exc:
        messages.append(f"external notify failed ({name}): {exc}")


def _run_external_probes(
    result: ReconcileResult,
    *,
    max_external: int,
    now_dt: datetime,
    dry_run: bool,
    in_quiet: bool,
    channel_available: bool,
    messages: list[str],
    counts: dict[str, int],
) -> None:
    waiting = [n for n in result.waiting_external if n.flow_type == "dag"]
    gated: list[ReconcileNode] = []
    for node in waiting:
        violations = _design_doc_gate(
            node.task_gid,
            body=node.body,
            actor="tasksmith-tick",
            dry_run=dry_run,
        )
        if violations:
            messages.append(
                f"design_doc_gate: {node.task_gid} → REVIEW ({len(violations)} violations)"
            )
            continue
        gated.append(node)

    if in_quiet:
        if gated:
            messages.append(language().messages["tick.skip_external_quiet"])
        return
    if not dry_run and not channel_available:
        if gated:
            messages.append("skip external: human_channel not installed")
        return

    due: list[tuple[datetime, ReconcileNode]] = []
    for node in gated:
        body = node.body
        if not isinstance(body, str):
            task = task_store().get_task(node.task_gid)
            body = task.get("body") if isinstance(task.get("body"), str) else None
        if not isinstance(body, str):
            continue
        sections = parse_sections(body)
        if sections is None:
            continue
        raw_at = parse_meta_field(sections, "check_at")
        if not raw_at:
            continue
        try:
            check_at = parse_check_at(raw_at)
        except ValueError:
            continue
        if check_at <= now_dt:
            due.append((check_at, node))

    due.sort(key=lambda item: item[0])
    selected = [node for _, node in due[:max_external]]
    texts = language().messages
    checked = 0

    for node in selected:
        if dry_run:
            messages.append(f"dry-run external: gid={node.task_gid}")
            continue

        task = task_store().get_task(node.task_gid)
        body = task.get("body")
        if not isinstance(body, str):
            continue
        sections = parse_sections(body)
        if sections is None:
            continue
        window = parse_meta_field(sections, "check_window")
        if window:
            try:
                if not in_window(now_dt, window):
                    next_at = next_window_start(now_dt, window)
                    body = update_meta_field(
                        body, "check_at", format_check_at(next_at), create=True
                    )
                    task_store().update_task(node.task_gid, body=body)
                    messages.append(
                        f"external gid={node.task_gid} deferred"
                    )
                    continue
            except ValueError:
                pass

        check_name = parse_meta_field(sections, "check") or ""
        probe = resolve_probe(check_name)
        params = parse_check_params(sections)
        check_every = _meta_int(sections, "check_every", default=60)
        check_max = _meta_int(sections, "check_max", default=5)
        checks = _meta_int(sections, "_checks", default=0)
        report_to = parse_meta_field(sections, "report_to")

        outcome = "deferred"
        try:
            if probe is None:
                raise RuntimeError("probe not found")
            probe_result = probe(task, params)
            status = (probe_result.status or "").strip().lower()
            report = (probe_result.report or "").strip()

            if status == "done":
                msg = report or texts["external.done"].format(name=node.name)
                _notify_external(node.name, msg, report_to, messages)
                transition_task(
                    node.task_gid,
                    state=PipelineState.DONE,
                    actor="tasksmith-tick",
                    execution_id=_execution_id("external-done", node.task_gid),
                    detail=report or msg,
                )
                outcome = "done"
            elif status in ("speak", "continue"):
                if status == "speak" and report:
                    _notify_external(node.name, report, report_to, messages)
                checks += 1
                body = update_meta_field(body, "_checks", str(checks), create=True)
                if checks >= check_max:
                    exhausted = texts["external.exhausted"].format(
                        name=node.name, count=checks
                    )
                    _notify_external(node.name, exhausted, report_to, messages)
                    task_store().update_task(node.task_gid, body=body)
                    transition_task(
                        node.task_gid,
                        state=PipelineState.BLOCKED,
                        actor="tasksmith-tick",
                        execution_id=_execution_id("external-blocked", node.task_gid),
                        detail=exhausted,
                    )
                    outcome = "blocked"
                else:
                    next_at = now_dt + timedelta(minutes=check_every)
                    body = update_meta_field(
                        body, "check_at", format_check_at(next_at), create=True
                    )
                    task_store().update_task(node.task_gid, body=body)
                    outcome = status
            else:
                raise ValueError(f"unexpected probe status: {status!r}")
        except Exception as exc:
            err_line = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            task_store().add_comment(
                node.task_gid,
                texts["external.probe_error"].format(error=err_line),
            )
            checks += 1
            body = update_meta_field(body, "_checks", str(checks), create=True)
            if checks >= check_max:
                exhausted = texts["external.exhausted"].format(
                    name=node.name, count=checks
                )
                _notify_external(node.name, exhausted, report_to, messages)
                task_store().update_task(node.task_gid, body=body)
                transition_task(
                    node.task_gid,
                    state=PipelineState.BLOCKED,
                    actor="tasksmith-tick",
                    execution_id=_execution_id("external-blocked", node.task_gid),
                    detail=exhausted,
                )
                outcome = "blocked"
            else:
                next_at = now_dt + timedelta(minutes=check_every)
                body = update_meta_field(
                    body, "check_at", format_check_at(next_at), create=True
                )
                task_store().update_task(node.task_gid, body=body)
                outcome = "continue"

        checked += 1
        messages.append(f"external gid={node.task_gid} {outcome}")

    if checked:
        counts["external_checked"] = counts.get("external_checked", 0) + checked


@dataclass(frozen=True)
class AdoptResult:
    adopted: list[str]  # GIDs moved to DONE (the target GIDs in dry-run)
    reopened: list[str]  # GIDs moved to REVIEW / READY (the target GIDs in dry-run)
    unregistered: list[str]  # GIDs whose pending was cleared
    errors: list[str]  # "gid: exception message" (one failure does not stop the others)
    dry_run: bool


def adopt_completions(
    result: ReconcileResult,
    *,
    max_adopt: int = 10,
    dry_run: bool = False,
) -> AdoptResult:
    """Adopt into the body state a completion / revert-to-incomplete a human made in the store (deterministic).

    adoptable_completions are processed first, adoptable_reopens with the remaining slots
    (max_adopt in total).
    """
    if max_adopt < 0:
        raise ValueError("--max-adopt must be 0 or greater")

    adopted: list[str] = []
    reopened: list[str] = []
    unregistered: list[str] = []
    errors: list[str] = []
    completions = result.adoptable_completions[:max_adopt]
    reopens = result.adoptable_reopens[: max_adopt - len(completions)]

    for node in completions:
        gid = node.task_gid
        if dry_run:
            adopted.append(gid)
            continue
        try:
            transition_task(
                gid,
                state=PipelineState.DONE,
                actor="tasksmith-adopt",
                execution_id=_execution_id("adopt-done", gid),
                detail=language().messages["adopt.done_detail"],
            )
            adopted.append(gid)
        except Exception as exc:
            errors.append(f"{gid}: {exc}")

    for node in reopens:
        gid = node.task_gid
        if dry_run:
            reopened.append(gid)
            continue
        target = (
            PipelineState.REVIEW if node.flow_type == "dag" else PipelineState.READY
        )
        try:
            task = task_store().get_task(gid)
            body = task.get("body")
            if isinstance(body, str) and is_controlled_notes(body):
                secs = parse_sections(body)
                if secs and parse_meta_field(secs, "_review_requested"):
                    task_store().update_task(
                        gid,
                        body=update_meta_field(body, "_review_requested", ""),
                    )
            transition_task(
                gid,
                state=target,
                actor="tasksmith-adopt",
                execution_id=_execution_id("adopt-reopen", gid),
                detail=language().messages["adopt.reopen_detail"],
            )
            reopened.append(gid)
        except Exception as exc:
            errors.append(f"{gid}: {exc}")

    return AdoptResult(
        adopted=adopted,
        reopened=reopened,
        unregistered=unregistered,
        errors=errors,
        dry_run=dry_run,
    )


SECTION_OUTCOMES: tuple[str, ...] = (
    "moved", "adopted", "classified", "inherited", "unresolved", "skipped",
)


@dataclass(frozen=True)
class SectionAssignment:
    task_gid: str
    name: str
    outcome: str  # moved | adopted | classified | inherited | unresolved | skipped
    section_id: str | None


def _decide_section(
    node: ReconcileNode,
    notes: str,
    *,
    config: Any,
    decided: dict[str, str | None],
) -> tuple[str, str | None, bool, bool]:
    """Decide (outcome, section_id, whether to write the body, whether to move) for one item (deterministic, no LLM)."""
    from tasksmith.sections import classify_section

    secs = parse_sections(notes) if notes else None
    body_section = parse_meta_field(secs, "section") if secs else None
    body_id = config.normalize_id(body_section) if body_section else None

    if node.parent_gid is not None:
        # a subtask does not belong to a section (sec. 9.5); only the body inherits the parent's id
        if body_id:
            return "inherited", body_id, False, False
        parent_id = decided.get(node.parent_gid) if node.parent_gid in decided else None
        inherited = parent_id or (config.normalize_id(node.section_hint) if node.section_hint else None)
        if inherited:
            return "inherited", inherited, True, False
        return "unresolved", None, True, False

    if body_id:
        return "moved", body_id, False, True
    hint_id = config.normalize_id(node.section_hint) if node.section_hint else None
    if hint_id:
        return "adopted", hint_id, True, False
    objective = _objective_text(secs) if secs else ""
    classified = classify_section(node.name, objective, config=config)
    sec_def = config.by_id(classified) if classified else None
    if sec_def is not None and not sec_def.default:
        return "classified", sec_def.id, True, True
    return "unresolved", None, True, False


def assign_sections(
    result: ReconcileResult,
    *,
    config: Any,
    max_section: int,
    dry_run: bool,
    now: datetime | None = None,
    warnings: list[str] | None = None,
) -> list[SectionAssignment]:
    """Fill in the body section: of unsectioned tasks and move parents in the default lane into a lane.

    Order: adopt (non-default lane in the store) -> classify (parents in the default lane only)
    -> inherit (subtasks). The cap max_section counts the items for which a write (body update
    or move) was done. state, owner and flow are not changed. One exception is pushed to
    warnings and processing goes on.
    """
    if max_section < 0:
        raise ValueError("--max-section must be 0 or greater")
    if max_section == 0:
        return []
    now_iso = (now or datetime.now(_tz())).astimezone(_tz()).isoformat(timespec="seconds")
    parents = [n for n in result.unsectioned if n.parent_gid is None]
    subs = [n for n in result.unsectioned if n.parent_gid is not None]
    unsectioned_parents = {n.task_gid for n in parents}
    decided: dict[str, str | None] = {}
    out: list[SectionAssignment] = []
    writes = 0

    for node in parents + subs:
        is_sub = node.parent_gid is not None
        if writes >= max_section or (
            is_sub and node.parent_gid in unsectioned_parents and node.parent_gid not in decided
        ):
            # cap reached, or the parent is unprocessed in this tick. The cap is not consumed
            if is_sub:
                out.append(SectionAssignment(node.task_gid, node.name, "skipped", None))
            continue
        try:
            if dry_run:
                notes = result.section_notes.get(node.task_gid, "")
            else:
                # re-fetch so that human edits made after reconcile are not overwritten
                task = task_store().get_task(node.task_gid)
                notes = task.get("body") if isinstance(task.get("body"), str) else ""
            outcome, section_id, write_body, move = _decide_section(
                node, notes, config=config, decided=decided
            )
            if not dry_run:
                if write_body:
                    if section_id:
                        new_notes = update_meta_field(notes, "section", section_id, create=True)
                    else:
                        new_notes = update_meta_field(
                            notes, _SECTION_UNRESOLVED_KEY, now_iso, create=True
                        )
                    task_store().update_task(node.task_gid, body=new_notes)
                if move and section_id:
                    task_store().update_task(
                        node.task_gid, section_gid=config.by_id(section_id).gid
                    )
        except Exception:
            _log.warning(
                "[tasksmith] assign_sections failed gid=%s", node.task_gid, exc_info=True
            )
            if warnings is not None:
                warnings.append(f"assign_sections warning gid={node.task_gid}")
            continue
        if write_body or move:
            writes += 1
        if not is_sub:
            decided[node.task_gid] = section_id
        out.append(SectionAssignment(node.task_gid, node.name, outcome, section_id))
    return out


def _section_counts(assignments: Sequence[SectionAssignment]) -> dict[str, int]:
    counts = {k: 0 for k in SECTION_OUTCOMES}
    for a in assignments:
        counts[a.outcome] = counts.get(a.outcome, 0) + 1
    return counts


def _assign_sections_step(
    result: ReconcileResult,
    *,
    max_section: int,
    dry_run: bool,
    now: datetime | None,
    counts: dict[str, int],
) -> list[str]:
    """For tick: call assign_sections and return the counts and the messages lines."""
    config = _load_sections_config()
    if config is None:
        return ["assign_sections skipped: sections config missing"]
    messages: list[str] = []
    assignments = assign_sections(
        result,
        config=config,
        max_section=max_section,
        dry_run=dry_run,
        now=now,
        warnings=messages,
    )
    by_outcome = _section_counts(assignments)
    sectioned = sum(by_outcome[k] for k in ("moved", "adopted", "classified", "inherited"))
    counts["sectioned"] = sectioned
    if dry_run:
        for a in assignments:
            messages.append(
                f"dry-run section gid={a.task_gid} outcome={a.outcome}"
                f" section={a.section_id or '-'} name={a.name}"
            )
    messages.append(
        f"{'dry-run ' if dry_run else ''}assign_sections: sectioned={sectioned}"
        f" unresolved={by_outcome['unresolved']} skipped={by_outcome['skipped']}"
    )
    return messages

def tick(
    project_gid: str | None = None,
    *,
    dry_run: bool = False,
    max_running: int = 1,
    max_human: int = 3,
    max_review: int = 1,
    max_rollup: int = 5,
    max_adopt: int = 10,
    max_section: int = 5,
    max_external: int = 3,
    metrics_path: Path | None = None,
    digest_state_path: Path | None = None,
    now: datetime | None = None,
    event_sink: EventSink | None = None,
    on_reconcile: Callable[[ReconcileResult], None] | None = None,
) -> TickResult:
    """Advance READY HUMAN / AI nodes and remind overdue WAITING_HUMAN nodes."""
    started = time.monotonic()
    def _emit(result: TickResult) -> TickResult:
        _append_metrics(
            {
                "kind": "tasksmith_tick",
                "timestamp": _utc_now_iso(),
                "status": result.status,
                "task_gid": result.task_gid,
                "elapsed_seconds": max(0.0, time.monotonic() - started),
                "message": result.message,
                "counts": result.counts,
                "warnings": list(result.warnings),
            },
            metrics_path=metrics_path,
        )
        return result

    try:
        return _emit(
            _tick_body(
                project_gid,
                dry_run=dry_run,
                max_running=max_running,
                max_human=max_human,
                max_review=max_review,
                max_rollup=max_rollup,
                max_adopt=max_adopt,
                max_section=max_section,
                max_external=max_external,
                metrics_path=metrics_path,
                digest_state_path=digest_state_path,
                now=now,
                event_sink=event_sink,
                on_reconcile=on_reconcile,
            )
        )
    except Exception as exc:
        _append_metrics(
            {
                "kind": "tasksmith_tick",
                "timestamp": _utc_now_iso(),
                "status": "error",
                "task_gid": None,
                "elapsed_seconds": max(0.0, time.monotonic() - started),
                "message": f"{type(exc).__name__}: {exc}",
            },
            metrics_path=metrics_path,
        )
        raise


def _tick_body(
    project_gid: str | None = None,
    *,
    dry_run: bool = False,
    max_running: int = 1,
    max_human: int = 3,
    max_review: int = 1,
    max_rollup: int = 5,
    max_adopt: int = 10,
    max_section: int = 5,
    max_external: int = 3,
    metrics_path: Path | None = None,
    digest_state_path: Path | None = None,
    now: datetime | None = None,
    event_sink: EventSink | None = None,
    on_reconcile: Callable[[ReconcileResult], None] | None = None,
) -> TickResult:
    """The tick body (called from the metrics wrapper)."""
    from tasksmith.human import (
        RequestHumanResult,
        _request_review_batch,
        build_human_request_text,
        post_review_digest,
        remind_waiting_humans,
        request_human,
    )
    from tasksmith.runtime import human_channel_installed

    if max_running < 1:
        raise ValueError("--max must be 1 or greater")
    if max_human < 0:
        raise ValueError("--max-human must be 0 or greater")
    if max_review < 0:
        raise ValueError("--max-review must be 0 or greater")
    if max_rollup < 0:
        raise ValueError("--max-rollup must be 0 or greater")
    if max_adopt < 0:
        raise ValueError("--max-adopt must be 0 or greater")
    if max_section < 0:
        raise ValueError("--max-section must be 0 or greater")
    if max_external < 0:
        raise ValueError("--max-external must be 0 or greater")
    result = reconcile(project_gid)
    if on_reconcile is not None:
        try:
            on_reconcile(result)
        except Exception:
            _log.exception("on_reconcile failed")

    counts = _reconcile_counts(result)
    warnings_t = tuple(result.warnings)
    st = settings()
    now_dt = now or datetime.now(_tz())
    quiet_hours = list(st.quiet_hours) if st.quiet_hours else None
    in_quiet = _is_quiet_hours(now_dt, quiet_hours) if quiet_hours else False
    messages: list[str] = []
    messages.extend(result.warnings)

    # count personal nodes (state sync only, not a reminder target)
    personal_review = [n for n in result.review if n.flow_type != "dag"]
    if personal_review:
        counts["personal"] = len(personal_review)

    # unsectioned: managed tasks with no section set / left in the default lane
    if result.unsectioned:
        counts["unsectioned"] = len(result.unsectioned)
        messages.append(language().messages["tick.unsectioned_count"].format(count=len(result.unsectioned)))

    # adopt nodes a human set back to completed / not completed in the store into state (parent completion is picked up on the next tick)
    adopt = adopt_completions(
        result, max_adopt=max_adopt, dry_run=dry_run
    )
    counts["adopted"] = len(adopt.adopted)
    counts["reopened"] = len(adopt.reopened)
    prefix = "dry-run " if dry_run else ""
    if adopt.adopted:
        messages.append(f"{prefix}adopted: {', '.join(adopt.adopted)}")
    if adopt.reopened:
        messages.append(f"{prefix}reopened: {', '.join(adopt.reopened)}")
    for err in adopt.errors:
        messages.append(f"adopt error: {err}")
    _block_failed_running_ai_nodes(project_gid, dry_run=dry_run, messages=messages)
    channel_available = human_channel_installed()
    if not dry_run and not channel_available:
        if result.waiting_human:
            messages.append("skip remind: human_channel not installed")
        reminded: list[str] = []
    else:
        reminded = remind_waiting_humans(
            result.waiting_human,
            remind_after_hours=int(st.remind_after_hours),
            remind_max_count=int(st.remind_max_count),
            dry_run=dry_run,
            project_gid=project_gid,
            now=now_dt,
        )
    if reminded:
        messages.append(f"reminded: {', '.join(reminded)}")

    # move completable parents to DONE (dry-run only reports)
    completed_parents: list[str] = []
    for node in result.completable_parents:
        if dry_run:
            messages.append(f"dry-run completable_parent: gid={node.task_gid} name={node.name!r}")
            continue
        transition_task(
            node.task_gid,
            state=PipelineState.DONE,
            actor="tasksmith-tick",
            execution_id=_execution_id("parent-done", node.task_gid),
            detail=language().messages["tick.parent_done_detail"],
        )
        completed_parents.append(node.task_gid)
        messages.append(f"completed_parent gid={node.task_gid}")

    # check_design_doc before selecting READY. Violating nodes go to REVIEW.
    # use the body already fetched by reconcile.
    gated_ready: list[ReconcileNode] = []
    for node in result.ready:
        violations = _design_doc_gate(
            node.task_gid,
            body=node.body,
            actor="tasksmith-tick",
            dry_run=dry_run,
        )
        if violations:
            messages.append(
                f"design_doc_gate: {node.task_gid} → REVIEW ({len(violations)} violations)"
            )
            continue
        gated_ready.append(node)

    _run_external_probes(
        result,
        max_external=max_external,
        now_dt=now_dt,
        dry_run=dry_run,
        in_quiet=in_quiet,
        channel_available=channel_available,
        messages=messages,
        counts=counts,
    )

    # right after design_doc_gate, before request_review: fill in the body section: and move lanes
    if max_section > 0 and result.unsectioned:
        messages.extend(
            _assign_sections_step(
                result,
                max_section=max_section,
                dry_run=dry_run,
                now=now_dt,
                counts=counts,
            )
        )

    review_after_elaborate = list(result.review)
    if in_quiet:
        if result.review or result.overdue:
            messages.append(language().messages["tick.skip_review_digest_quiet"])
    elif not dry_run and not channel_available:
        if result.review or result.overdue:
            messages.append("skip review/digest: human_channel not installed")
    else:
        messages.extend(
            _request_review_batch(
                result.review,
                project_gid=project_gid,
                max_review=max_review,
                dry_run=dry_run,
                now=now,
                event_sink=event_sink,
            )
        )
        messages.extend(
            post_review_digest(
                review=review_after_elaborate,
                overdue=result.overdue,
                inverted_periods=getattr(result, "inverted_periods", None) or [],
                dry_run=dry_run,
                digest_state_path=digest_state_path,
                now=now,
            )
        )

    human_ready = [n for n in gated_ready if n.owner_type == OwnerType.HUMAN.value and n.flow_type == "dag"]
    counts["human_ready"] = len(human_ready)
    ai_ready = [n for n in gated_ready if n.owner_type == OwnerType.AI.value and n.flow_type == "dag"]

    human_result: RequestHumanResult | None = None
    waiting_count = len(result.waiting_human)
    if human_ready and waiting_count < max_human and not in_quiet:
        node = human_ready[0]
        channel_id = st.request_channel_id
        channel_ok = isinstance(channel_id, str) and bool(channel_id.strip())
        if not channel_ok:
            messages.append(language().messages["tick.skip_human_no_channel"])
        elif not channel_available:
            messages.append("skip human channel: human_channel not installed")
        elif dry_run:
            task = task_store().get_task(node.task_gid)
            secs = parse_sections(task.get("body") if isinstance(task.get("body"), str) else "")
            preview = (
                build_human_request_text(task_name=node.name, sections=secs or {})
                if secs
                else ""
            )
            human_result = RequestHumanResult(
                task_gid=node.task_gid,
                channel_id=channel_id if isinstance(channel_id, str) else None,
                thread_ts=None,
                request_text=preview,
                dry_run=True,
            )
            messages.append(f"dry-run human: gid={node.task_gid} name={node.name!r}")
        else:
            human_result = request_human(
                node.task_gid,
                project_gid=project_gid,
                dry_run=False,
                event_sink=event_sink,
            )
            messages.append(
                f"requested_human gid={human_result.task_gid} channel={human_result.channel_id} ts={human_result.thread_ts}"
            )
    elif human_ready and waiting_count >= max_human:
        messages.append(
            language().messages["tick.skip_human_cap"].format(
                waiting_count=waiting_count, max_human=max_human
            )
        )

    # AI path
    running = _count_running_from_project(project_gid)
    if ai_ready and running < max_running:
        node = ai_ready[0]
        task = task_store().get_task(node.task_gid)
        body = task.get("body")
        sections = parse_sections(body) if isinstance(body, str) else None
        order_preview = ""
        skill_name: str | None = None
        if sections:
            skill_name, skill_err = _resolve_skill_executor(sections)
            if skill_err:
                messages.append(f"ai_executor: {node.task_gid} → BLOCKED ({skill_err})")
                if not dry_run:
                    transition_task(
                        node.task_gid,
                        state=PipelineState.BLOCKED,
                        actor="tasksmith-tick",
                        execution_id=_execution_id("tick", node.task_gid),
                        detail=skill_err,
                    )
                return TickResult(
                    status="noop",
                    task_gid=node.task_gid,
                    name=node.name,
                    reminded_gids=tuple(reminded),
                    counts=counts,
                    warnings=warnings_t,
                    message="\n".join(messages),
                )
            if skill_name:
                order_preview = _build_run_prompt(sections, skill_name=skill_name)
        if dry_run:
            messages.append(f"dry-run ai: gid={node.task_gid} name={node.name!r}")
            status = "dry_run"
            return TickResult(
                status=status,
                task_gid=node.task_gid,
                name=node.name,
                order_preview=order_preview,
                request_text=human_result.request_text if human_result else None,
                reminded_gids=tuple(reminded),
                counts=counts,
                warnings=warnings_t,
                message=(
                    "\n".join(messages)
                    + (f"\n--- order ---\n{order_preview}" if order_preview else "")
                    + (
                        f"\n--- human request ---\n{human_result.request_text}"
                        if human_result
                        else ""
                    )
                ).strip(),
            )

        if sections is None or not skill_name:
            raise ValueError("Cannot interpret the AI node body")
        generation = _next_generation(node.task_gid)
        meta_digest = _control_meta_digest(sections)
        idem_key = f"tasksmith:run:{node.task_gid}:{generation}"
        prompt = _build_run_prompt(sections, skill_name=skill_name)
        transition_task(
            node.task_gid,
            state=PipelineState.RUNNING,
            actor="tasksmith-tick",
            execution_id=_execution_id("tick", node.task_gid),
            detail=language().messages["tick.running_detail"].format(generation=generation),
        )
        llm_uuid = executor().submit(
            node.task_gid,
            prompt,
            idempotency_key=idem_key,
            meta_digest=meta_digest,
        )
        if llm_uuid is None:
            messages.append(language().messages["tick.skip_enqueue_key_in_use"].format(task_gid=node.task_gid))
        else:
            messages.append(f"enqueued gid={node.task_gid} llm_uuid={llm_uuid} key={idem_key}")
        return TickResult(
            status="enqueued",
            task_gid=node.task_gid,
            name=node.name,
            order_path=None,
            idempotency_key=idem_key,
            order_preview=order_preview,
            request_text=human_result.request_text if human_result else None,
            reminded_gids=tuple(reminded),
            counts=counts,
            warnings=warnings_t,
            message="\n".join(messages),
        )
    elif ai_ready and running >= max_running:
        messages.append(
            language().messages["tick.skip_ai_cap"].format(running=running, max_running=max_running)
        )

    # parent due-date rollup: inspect up to the first max_rollup managed parents
    rolled_up_parents_count = 0
    if max_rollup > 0 and result.sortable_parent_gids:
        from tasksmith.transition import rollup_parent_due as _rollup_parent_due

        for parent_gid in result.sortable_parent_gids[:max_rollup]:
            try:
                rollup_result = _rollup_parent_due(parent_gid, dry_run=dry_run)
                if rollup_result.changed:
                    rolled_up_parents_count += 1
                    messages.append(
                        f"rolled_up_due gid={parent_gid} "
                        f"{rollup_result.before}->{rollup_result.after}"
                    )
            except Exception:
                _log.warning(
                    "[tasksmith] rollup_parent_due failed in tick parent=%s", parent_gid, exc_info=True
                )
                messages.append(f"rollup_parent_due warning gid={parent_gid}")
    if rolled_up_parents_count:
        counts["rolled_up_parents"] = rolled_up_parents_count
    if human_result is not None:
        return TickResult(
            status="dry_run" if dry_run else "requested_human",
            task_gid=human_result.task_gid,
            name=next((n.name for n in human_ready if n.task_gid == human_result.task_gid), None),
            request_text=human_result.request_text,
            reminded_gids=tuple(reminded),
            counts=counts,
            warnings=warnings_t,
            message="\n".join(messages) or f"requested_human gid={human_result.task_gid}",
        )
    if reminded:
        return TickResult(
            status="reminded",
            reminded_gids=tuple(reminded),
            counts=counts,
            warnings=warnings_t,
            message="\n".join(messages),
        )
    if messages:
        return TickResult(
            status="noop",
            reminded_gids=tuple(reminded),
            counts=counts,
            warnings=warnings_t,
            message="\n".join(messages),
        )
    return TickResult(
        status="noop",
        counts=counts,
        warnings=warnings_t,
        message=language().messages["tick.noop_no_ready"],
    )

def _parse_run_markers(text: str) -> tuple[str | None, list[str], list[int]]:
    status: str | None = None
    for m in _PIPELINE_STATUS_RE.finditer(text or ""):
        status = m.group(1)
    notes = [m.strip() for m in _NOTE_RE.findall(text or "") if m.strip()]
    ac_indices: list[int] = []
    for m in _AC_DONE_RE.finditer(text or ""):
        for part in m.group(1).split(","):
            part = part.strip()
            if part.isdigit():
                ac_indices.append(int(part))
    return status, notes, ac_indices


def _apply_ac_done(items: list[str], indices: list[int]) -> list[str]:
    wanted = set(indices)
    out: list[str] = []
    for i, line in enumerate(items, start=1):
        if i in wanted:
            if re.match(r"^\[ \]", line):
                line = "[x]" + line[3:]
            elif re.match(r"^\[x\]", line, re.I):
                pass
            else:
                line = f"[x] {line}"
        out.append(line)
    return out



def _build_run_prompt(sections: dict[str, str], *, skill_name: str) -> str:
    objective = _objective_text(sections)
    deliverables = sections.get("deliverables") or ""
    procedure = sections.get("procedure") or ""
    draft = sections.get("draft") or ""
    acceptance = "\n".join(section_items(sections.get("acceptance") or ""))
    parts = [
        "# tasksmith node\n\n",
        f"skill: {skill_name}\n\n",
        language().prompts["run_prompt.heading_objective"],
        f"{objective}\n\n",
    ]
    if deliverables.strip():
        parts.extend([language().prompts["run_prompt.heading_deliverables"], f"{deliverables}\n\n"])
    if procedure.strip():
        parts.extend([language().prompts["run_prompt.heading_procedure"], f"{procedure}\n\n"])
    parts.extend(
        [
            language().prompts["run_prompt.heading_draft"],
            f"{draft}\n\n",
            language().prompts["run_prompt.heading_acceptance"],
            f"{acceptance}\n\n",
            language().prompts["run_prompt.heading_contract"],
            language().prompts["run_prompt.contract_intro"],
            language().prompts["run_prompt.contract_done"],
            language().prompts["run_prompt.contract_blocked"],
            language().prompts["run_prompt.contract_note"],
            language().prompts["run_prompt.contract_ac_done"],
            "\n",
            language().prompts["run_prompt.heading_forbidden"],
            language().prompts["run_prompt.forbidden_fields"],
            language().prompts["run_prompt.forbidden_outputs"],
        ]
    )
    return "".join(parts)


@dataclass(frozen=True)
class FinishNodeResult:
    status: str  # DONE | BLOCKED | REVIEW
    reason: str
    task_gid: str


def finish_node(
    task_gid: str,
    *,
    result_path: Path,
    meta_digest: str,
    event_sink: EventSink | None = None,
) -> FinishNodeResult:
    """Take in the result of the LLM job and transition to DONE / BLOCKED / REVIEW. Never fail with an exception."""
    exec_id = _execution_id("finish", task_gid)
    task_meta: dict[str, Any] = {}

    def _emit_finish(
        state: PipelineState,
        reason: str,
        *,
        executor_name: str | None = None,
        notes: Sequence[str] = (),
        ac_indices: Sequence[int] = (),
    ) -> None:
        if state == PipelineState.DONE:
            kind: EventKind = "node_done"
            outcome = "; ".join(notes[:3]) or language().messages["finish_node.none"]
            acs = ",".join(str(i) for i in ac_indices) or language().messages["finish_node.none"]
            summary = language().messages["events.finish_node.done_summary"].format(executor_name=executor_name, outcome=outcome, acs=acs)
        else:
            kind = "node_review" if state == PipelineState.REVIEW else "node_blocked"
            summary = language().messages["events.finish_node.stopped_summary"].format(state=state.value, reason=first_line(reason))
            if executor_name:
                summary += language().messages["events.finish_node.executor_suffix"].format(executor_name=executor_name)
        _emit_event(
            event_sink,
            kind,
            task_gid=task_gid,
            task_name=task_meta.get("name"),
            permalink_url=task_meta.get("permalink_url"),
            summary=summary,
            execution_id=exec_id,
            actor="tasksmith-finish-node",
        )

    try:
        task = task_store().get_task(task_gid)
        task_meta.update(task)
        body = task.get("body")
        if not isinstance(body, str):
            raise ValueError("Task has no body")
        sections = parse_sections(body)
        if sections is None or not is_controlled_notes(body):
            raise ValueError("Body is not in the fixed format")

        executor_name = parse_meta_field(sections, "executor")
        if not result_path.is_file():
            reason = language().messages["finish_node.result_missing"].format(result_path=result_path)
            transition_task(
                task_gid,
                state=PipelineState.BLOCKED,
                actor="tasksmith-finish-node",
                execution_id=exec_id,
                detail=reason,
            )
            _emit_finish(PipelineState.BLOCKED, reason, executor_name=executor_name)
            return FinishNodeResult(status="BLOCKED", reason=reason, task_gid=task_gid)

        try:
            stdout = result_path.read_text(encoding="utf-8")
        except OSError as exc:
            reason = language().messages["finish_node.result_unreadable"].format(error=exc)
            transition_task(
                task_gid,
                state=PipelineState.BLOCKED,
                actor="tasksmith-finish-node",
                execution_id=exec_id,
                detail=reason,
            )
            _emit_finish(PipelineState.BLOCKED, reason, executor_name=executor_name)
            return FinishNodeResult(status="BLOCKED", reason=reason, task_gid=task_gid)

        status, notes, ac_indices = _parse_run_markers(stdout)
        if status is None:
            reason = language().messages["finish_node.marker_missing"]
            transition_task(
                task_gid,
                state=PipelineState.BLOCKED,
                actor="tasksmith-finish-node",
                execution_id=exec_id,
                detail=reason,
            )
            _emit_finish(PipelineState.BLOCKED, reason, executor_name=executor_name)
            return FinishNodeResult(status="BLOCKED", reason=reason, task_gid=task_gid)

        current_digest = _control_meta_digest(sections)
        state_now = parse_meta_field(sections, "state")
        meta_conflict = (
            current_digest != meta_digest
            or state_now != PipelineState.RUNNING.value
        )
        if meta_conflict:
            reason = "control metadata changed during run"
            transition_task(
                task_gid,
                state=PipelineState.REVIEW,
                actor="tasksmith-finish-node",
                execution_id=exec_id,
                detail=reason,
            )
            _emit_finish(PipelineState.REVIEW, reason, executor_name=executor_name)
            return FinishNodeResult(status="REVIEW", reason=reason, task_gid=task_gid)

        fresh_task = task_store().get_task(task_gid)
        fresh_notes = fresh_task.get("body")
        if not isinstance(fresh_notes, str):
            raise ValueError("Re-fetched task has no body")
        fresh_sections = parse_sections(fresh_notes)
        if fresh_sections is None:
            raise ValueError("Re-fetched body is not in the fixed format")

        body = fresh_notes
        sections = fresh_sections
        draft = sections.get("draft") or ""
        if notes:
            addition = "\n".join(notes)
            draft = f"{draft}\n{addition}".strip() if draft.strip() else addition
        acceptance_items = section_items(sections.get("acceptance") or "")
        if ac_indices:
            acceptance_items = _apply_ac_done(acceptance_items, ac_indices)

        if notes or ac_indices:
            updated = replace_editable_sections(
                body,
                draft=draft if notes else None,
                acceptance=acceptance_items if ac_indices else None,
            )
            task_store().update_task(task_gid, body=updated)

        target = PipelineState.DONE if status == "DONE" else PipelineState.BLOCKED
        reason = "LLM marker BLOCKED" if status == "BLOCKED" else f"finish-node → {target.value}"
        transition_task(
            task_gid,
            state=target,
            actor="tasksmith-finish-node",
            execution_id=exec_id,
            detail=reason,
        )
        _emit_finish(
            target,
            reason,
            executor_name=executor_name,
            notes=notes,
            ac_indices=ac_indices,
        )
        return FinishNodeResult(status=target.value, reason=reason, task_gid=task_gid)
    except Exception as exc:  # noqa: BLE001
        try:
            transition_task(
                task_gid,
                state=PipelineState.BLOCKED,
                actor="tasksmith-finish-node",
                execution_id=exec_id,
                detail=language().messages["finish_node.exception"].format(error=exc),
            )
        except Exception as inner:  # noqa: BLE001
            return FinishNodeResult(
                status="BLOCKED",
                reason=language().messages["finish_node.exception_transition_failed"].format(error=exc, inner=inner),
                task_gid=task_gid,
            )
        _emit_finish(PipelineState.BLOCKED, language().messages["finish_node.exception"].format(error=exc))
        return FinishNodeResult(status="BLOCKED", reason=language().messages["finish_node.exception"].format(error=exc), task_gid=task_gid)


@dataclass(frozen=True)
class RecoverResult:
    status: str  # recovered | error
    task_gid: str
    message: str


def recover(
    task_gid: str,
    *,
    project_gid: str | None = None,
) -> RecoverResult:
    """BLOCKED -> READY. No transition from any other state."""
    task = task_store().get_task(
        task_gid
    )
    body = task.get("body")
    if not isinstance(body, str):
        return RecoverResult(
            status="error", task_gid=task_gid, message=language().messages["recover.error_no_body"]
        )
    sections = parse_sections(body)
    if sections is None or not is_controlled_notes(body):
        return RecoverResult(
            status="error",
            task_gid=task_gid,
            message=language().messages["recover.error_not_fixed_format"],
        )
    state = parse_meta_field(sections, "state")
    if state != PipelineState.BLOCKED.value:
        return RecoverResult(
            status="error",
            task_gid=task_gid,
            message=language().messages["recover.error_not_blocked"].format(state=state),
        )
    transition_task(
        task_gid,
        state=PipelineState.READY,
        actor="tasksmith-recover",
        execution_id=_execution_id("recover", task_gid),
        detail="recover: BLOCKED → READY",
    )
    return RecoverResult(
        status="recovered",
        task_gid=task_gid,
        message=f"recovered: {task_gid} BLOCKED → READY",
    )


def _parse_metrics_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        if text.endswith("Z"):
            return datetime.fromisoformat(text[:-1] + "+00:00")
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _nearest_rank(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile (the ceil(pct/100*n)-th after sorting). None when empty."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[rank - 1]


def _numeric_field(rows: list[dict[str, Any]], key: str) -> list[float]:
    out: list[float] = []
    for row in rows:
        v = row.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out.append(float(v))
    return out


def aggregate_metrics(
    metrics_path: Path | None = None,
    *,
    days: int = 7,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate the tasksmith metrics JSONL (deterministic; fixture gids are excluded)."""
    path = metrics_path if metrics_path is not None else _default_metrics_path()
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    cutoff = clock - timedelta(days=days)

    fixture_excluded = 0
    tick_rows: list[dict[str, Any]] = []
    human_rows: list[dict[str, Any]] = []
    template_rows: list[dict[str, Any]] = []

    if path.exists():
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                task_gid = record.get("task_gid")
                if isinstance(task_gid, str) and task_gid and not task_gid.isdigit():
                    fixture_excluded += 1
                    continue
                ts = _parse_metrics_timestamp(record.get("timestamp"))
                if ts is None:
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < cutoff:
                    continue
                kind = record.get("kind")
                if kind == "tasksmith_tick":
                    tick_rows.append(record)
                elif kind == "tasksmith_human_reply":
                    human_rows.append(record)
                elif kind == "tasksmith_template_tick":
                    template_rows.append(record)

    tick_rows.sort(
        key=lambda r: _parse_metrics_timestamp(r.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc)
    )
    tick_gaps: list[dict[str, Any]] = []
    for prev, cur in zip(tick_rows, tick_rows[1:]):
        t0 = _parse_metrics_timestamp(prev.get("timestamp"))
        t1 = _parse_metrics_timestamp(cur.get("timestamp"))
        if t0 is None or t1 is None:
            continue
        gap_min = (t1 - t0).total_seconds() / 60.0
        if gap_min >= 15:
            tick_gaps.append(
                {
                    "start": prev.get("timestamp"),
                    "end": cur.get("timestamp"),
                    "gap_minutes": int(gap_min) if gap_min == int(gap_min) else round(gap_min, 1),
                }
            )

    tick_status: dict[str, int] = defaultdict(int)
    for row in tick_rows:
        status = row.get("status")
        if isinstance(status, str):
            tick_status[status] += 1

    human_decisions: dict[str, int] = defaultdict(int)
    unclear = 0
    violations_nonempty = 0
    for row in human_rows:
        decision = row.get("decision")
        if isinstance(decision, str):
            human_decisions[decision] += 1
            if decision == "unclear":
                unclear += 1
        violations = row.get("violations")
        if isinstance(violations, list) and violations:
            violations_nonempty += 1

    human_count = len(human_rows)
    unclear_rate = (unclear / human_count) if human_count else 0.0
    violations_rate = (violations_nonempty / human_count) if human_count else 0.0

    review_daily: dict[str, int] = {}
    for row in tick_rows:
        ts = _parse_metrics_timestamp(row.get("timestamp"))
        if ts is None:
            continue
        day = ts.astimezone(timezone.utc).strftime("%Y-%m-%d")
        counts = row.get("counts") if isinstance(row.get("counts"), dict) else {}
        review = counts.get("review", 0)
        try:
            review_n = int(review)
        except (TypeError, ValueError):
            review_n = 0
        prev = review_daily.get(day)
        if prev is None or review_n > prev:
            review_daily[day] = review_n

    created_total = 0
    error_total = 0
    for row in template_rows:
        try:
            created_total += int(row.get("created_count") or 0)
        except (TypeError, ValueError):
            pass
        try:
            error_total += int(row.get("error_count") or 0)
        except (TypeError, ValueError):
            pass

    tick_elapsed = _numeric_field(tick_rows, "elapsed_seconds")

    return {
        "period_days": days,
        "tick_count": len(tick_rows),
        "tick_elapsed_p50": _nearest_rank(tick_elapsed, 50),
        "tick_elapsed_p95": _nearest_rank(tick_elapsed, 95),
        "tick_gaps": tick_gaps,
        "tick_status": dict(tick_status),
        "human_reply_count": human_count,
        "human_reply_decisions": dict(human_decisions),
        "unclear_rate": unclear_rate,
        "violations_rate": violations_rate,
        "review_daily_max": dict(sorted(review_daily.items())),
        "template_tick": {
            "count": len(template_rows),
            "created_total": created_total,
            "error_total": error_total,
        },
        "fixture_excluded": fixture_excluded,
    }


def format_metrics_text(summary: dict[str, Any]) -> str:
    """Human-readable output of aggregate_metrics."""
    lines = [
        f"period_days: {summary['period_days']}",
        f"tick_count: {summary['tick_count']}",
        f"fixture_excluded: {summary['fixture_excluded']}",
        "tick_status:",
    ]
    for status, n in sorted(summary.get("tick_status", {}).items()):
        lines.append(f"  {status}: {n}")
    lines.append(
        language().messages["metrics.tick_elapsed"].format(p50=summary.get('tick_elapsed_p50'), p95=summary.get('tick_elapsed_p95'))
    )
    lines.append("tick_gaps:")
    gaps = summary.get("tick_gaps") or []
    if not gaps:
        lines.append("  (none)")
    else:
        for g in gaps:
            lines.append(
                f"  {g.get('start')} → {g.get('end')} ({g.get('gap_minutes')} min)"
            )
    lines.append(f"human_reply_count: {summary['human_reply_count']}")
    lines.append("human_reply_decisions:")
    for decision, n in sorted(summary.get("human_reply_decisions", {}).items()):
        lines.append(f"  {decision}: {n}")
    lines.append(f"unclear_rate: {summary['unclear_rate']}")
    lines.append(f"violations_rate: {summary['violations_rate']}")
    lines.append("review_daily_max:")
    review = summary.get("review_daily_max") or {}
    if not review:
        lines.append("  (none)")
    else:
        for day, n in review.items():
            lines.append(f"  {day}: {n}")
    tt = summary.get("template_tick") or {}
    lines.append(
        "template_tick: "
        f"count={tt.get('count', 0)} created_total={tt.get('created_total', 0)} "
        f"error_total={tt.get('error_total', 0)}"
    )
    return "\n".join(lines)


