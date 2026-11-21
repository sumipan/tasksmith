"""Human interaction: requests, replies, reminders, review digest."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from tasksmith.body import (
    build_controlled_notes,
    is_controlled_notes,
    parse_meta_field,
    parse_sections,
    parse_start_on,
    remove_meta_field,
    replace_editable_sections,
    section_items,
    serialize_sections,
    update_meta_field,
)
from tasksmith.events import EventSink, first_line
from tasksmith.execution import EngineFailed, EngineUnavailable, thinker
from tasksmith.gates import check_design_doc, format_violations_comment
from tasksmith.language import language
from tasksmith.model import OwnerType, PipelineState
from tasksmith.reconcile import (
    ReconcileNode,
    _body_depends_on,
    _body_meta,
    _index_all_tasks,
    _load_sections_config,
    _project_gid,
)
from tasksmith.runtime import human_channel, human_channel_installed, task_store
from tasksmith.settings import settings
from tasksmith.tick import (
    _append_metrics,
    _emit_event,
    _execution_id,
    _parse_hhmm,
    _tz,
    _utc_now_iso,
)
from tasksmith.transition import pipeline_status_comment, transition_task

_log = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def _extract_payload(text: str) -> dict[str, Any] | None:
    """Extract one JSON object from LLM output (fenced block first, else outermost {...})."""
    if not text:
        return None
    candidates: list[str] = [m.group(1) for m in _FENCE_RE.finditer(text)]
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for cand in candidates:
        try:
            data = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


@runtime_checkable
class HumanChannel(Protocol):
    def ask(self, text: str) -> str: ...

    def follow_up(self, ref: str, text: str) -> None: ...

    def announce(self, text: str) -> None: ...

    def link(self, ref: str) -> str | None: ...


__all__ = [
    "HumanChannel",
    "RequestHumanResult",
    "RequestReviewResult",
    "RecordHumanReplyResult",
    "build_human_request_text",
    "request_human",
    "build_review_request_text",
    "request_review",
    "post_review_digest",
    "collect_human_reply_violations",
    "record_human_reply",
    "remind_waiting_humans",
    "parse_judgment_choices",
]

@dataclass(frozen=True)
class RequestHumanResult:
    task_gid: str
    channel_id: str | None
    thread_ts: str | None
    request_text: str
    dry_run: bool


@dataclass(frozen=True)
class RequestReviewResult:
    task_gid: str
    channel_id: str | None
    thread_ts: str | None
    request_text: str
    dry_run: bool
    skipped: bool = False
    skip_reason: str = ""


@dataclass(frozen=True)
class RecordHumanReplyResult:
    task_gid: str
    decision: str
    choice: str | None
    note: str | None
    transitioned_to: str
    violations: list[str] = field(default_factory=list)


_HUMAN_DECISIONS = ("approve", "done", "revise", "reject", "defer", "unclear", "fill", "replan")


def _judgment_heading() -> str:
    """Escaped heading text of the judgments section (from the language pack)."""
    return re.escape(language().headings["judgments"])


def _judgment_heading_re() -> re.Pattern[str]:
    return re.compile(
        rf"^#{{2,3}}[ \t]*{_judgment_heading()}[ \t]*$\n?(.*?)(?=^#{{2,3}}[ \t]|\Z)",
        re.S | re.M,
    )


def _colon_class() -> str:
    return "".join(re.escape(c) for c in language().word_lists["colons"])


def _choice_label_re() -> re.Pattern[str]:
    lead = "".join(re.escape(c) for c in language().word_lists["choice_label_lead"])
    return re.compile(rf"(?:^|[\s{lead}])([A-Z])\s*[{_colon_class()}]")


def _recommend_re() -> re.Pattern[str]:
    words = "|".join(re.escape(w) for w in language().word_lists["recommend"])
    return re.compile(rf"(?:{words})\s*[{_colon_class()}]?\s*([A-Z])", re.I)


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_REMINDED_MARKER = "PIPELINE_STATUS: REMINDED"
_DIGEST_STATE_NAME = "digest.json"


def _meta_request_id(sections: dict[str, str] | None) -> str | None:
    """Read _request_id, falling back to legacy _andon_id while WAITING_HUMAN tasks remain."""
    if not sections:
        return None
    rid = parse_meta_field(sections, "_request_id")
    if rid:
        return str(rid)
    legacy = parse_meta_field(sections, "_andon_id")
    return str(legacy) if legacy else None



def _objective_text(sections: dict[str, str]) -> str:
    return sections.get("objective") or ""


def _judgment_text(sections: dict[str, str]) -> str:
    """The judgments section; if empty, the legacy block inside the objective."""
    if "judgments" in sections:
        direct = (sections.get("judgments") or "").strip()
        if direct:
            return direct
    return _extract_judgment_block(_objective_text(sections))


def _extract_judgment_block(objective_text: str) -> str:
    plain = objective_text or ""
    m = _judgment_heading_re().search(plain)
    if m:
        return m.group(1).strip()
    # fallback when there is no heading line
    m2 = re.search(rf"###\s*{_judgment_heading()}\s*(.*)", plain, re.S)
    if m2:
        return m2.group(1).strip()
    return ""


def parse_judgment_choices(objective_or_judgment_text: str) -> list[str]:
    """Return the choice labels (A, B, ...) from the judgments text."""
    block = _extract_judgment_block(objective_or_judgment_text)
    if not block:
        # for a standalone section the body itself has no heading
        block = objective_or_judgment_text.strip()
    if not block:
        return []
    # drop "recommend: A" lines first, since a recommendation is not a choice, then collect the labels
    block_wo_rec = _recommend_re().sub("", block)
    labels: list[str] = []
    seen: set[str] = set()
    for m in _choice_label_re().finditer(block_wo_rec):
        label = m.group(1).upper()
        if label not in seen:
            seen.add(label)
            labels.append(label)
    return labels


def build_human_request_text(
    *,
    task_name: str,
    sections: dict[str, str],
) -> str:
    """Deterministically build the request text for a human from the sections (no LLM needed). Handles both v1/v2."""
    objective_text = _objective_text(sections)
    deliverables_text = sections.get("deliverables") or ""
    acceptance_text = sections.get("acceptance") or ""
    objective_plain = objective_text.strip()
    obj_cut = re.split(rf"###\s*{_judgment_heading()}", objective_plain, maxsplit=1)[0].strip()
    if not obj_cut:
        obj_cut = _judgment_heading_re().sub("", objective_plain).strip() or objective_plain
    deliverables_plain = deliverables_text.strip()
    if deliverables_plain:
        obj_cut = f"{obj_cut}\n\n{deliverables_plain}".strip() if obj_cut else deliverables_plain

    ac_items = section_items(acceptance_text)
    ac_lines = "\n".join(f"- {x}" for x in ac_items) if ac_items else language().messages["human_request.no_acceptance"]

    lines = [
        language().messages["human_request.title"].format(task_name=task_name),
        "",
        language().messages["human_request.heading_objective"],
        obj_cut or language().messages["human_request.no_objective"],
        "",
        language().messages["human_request.heading_acceptance"],
        ac_lines,
    ]
    judgment = _judgment_text(sections)
    if judgment:
        lines.extend(["", language().messages["human_request.heading_judgments"], judgment])
        rec = _recommend_re().search(judgment)
        choices = parse_judgment_choices(judgment)
        if choices:
            lines.append("")
            lines.append(language().messages["human_request.choices_prefix"] + " / ".join(choices))
        if rec:
            lines.append(language().messages["human_request.recommendation"].format(label=rec.group(1).upper()))
    lines.extend(
        [
            "",
            language().messages["human_request.footer"],
        ]
    )
    return "\n".join(lines)



def request_human(
    task_gid: str,
    *,
    project_gid: str | None = None,
    dry_run: bool = False,
    event_sink: EventSink | None = None,
) -> RequestHumanResult:
    """Put a HUMAN node into WAITING_HUMAN, request via the human channel and register it in pending."""
    from tasksmith import pending as loops_pending

    ch = settings().request_channel_id
    task = task_store().get_task(task_gid)
    body = task.get("body")
    if not isinstance(body, str) or not is_controlled_notes(body):
        raise ValueError("Body is not in the fixed format")
    sections = parse_sections(body)
    if sections is None:
        raise ValueError("Body is not in the fixed format")
    owner = parse_meta_field(sections, "owner")
    if owner != OwnerType.HUMAN.value:
        raise ValueError(f"owner is not HUMAN (current: {owner})")

    task_name = str(task.get("name") or task_gid)
    request_text = build_human_request_text(task_name=task_name, sections=sections)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if dry_run:
        return RequestHumanResult(
            task_gid=task_gid,
            channel_id=ch if isinstance(ch, str) else None,
            thread_ts=None,
            request_text=request_text,
            dry_run=True,
        )

    if not ch:
        raise ValueError(
            "request_channel_id is not set (tasksmith or asana_conversation.channel_ids in configs/secretary.yaml)"
        )
    if not human_channel_installed():
        raise RuntimeError("human channel is not installed")

    exec_id = _execution_id("request-human", task_gid)
    notes_with_since = update_meta_field(body, "_waiting_since", now_iso, create=True)
    notes_with_since = update_meta_field(notes_with_since, "_reminded_count", "0", create=True)
    notes_with_since = update_meta_field(notes_with_since, "_request_id", exec_id, create=True)
    task_store().update_task(task_gid, body=notes_with_since)

    transition_task(
        task_gid,
        state=PipelineState.WAITING_HUMAN,
        actor="tasksmith-request-human",
        execution_id=exec_id,
        detail="request_human: READY → WAITING_HUMAN",
    )

    ref = human_channel().ask(request_text)
    loops_pending.register(ref, f"tasksmith:{task_gid}")
    loops_pending.set_question(ref, task_gid)
    _emit_event(
        event_sink,
        "human_requested",
        task_gid=task_gid,
        task_name=task.get("name"),
        permalink_url=task.get("permalink_url"),
        summary=(
            language().messages["events.request_human.summary"].format(reason=first_line(request_text))
        ),
        execution_id=exec_id,
        actor="tasksmith-request-human",
    )

    permalink = human_channel().link(ref) or ""
    _judgment_choices = parse_judgment_choices(_judgment_text(sections) or "")
    options = _judgment_choices or ["resume", "abandon"]
    detail = request_text
    if permalink:
        detail = language().messages["request_human.detail_with_link"].format(detail=detail, permalink=permalink)
    detail = language().messages["request_human.detail_with_options"].format(detail=detail, exec_id=exec_id) + " / ".join(options)
    task_store().add_comment(
        task_gid,
        pipeline_status_comment(
            state=PipelineState.WAITING_HUMAN,
            actor="tasksmith-request-human",
            execution_id=_execution_id("request-human-comment", task_gid),
            detail=detail,
        ),
    )
    channel_id, _, thread_ts = ref.partition(":")
    return RequestHumanResult(
        task_gid=task_gid,
        channel_id=channel_id or None,
        thread_ts=thread_ts or None,
        request_text=request_text,
        dry_run=False,
    )



def _jst_today(now: datetime | date | None = None) -> date:
    if now is None:
        return datetime.now(_tz()).date()
    if isinstance(now, datetime):
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.astimezone(_tz()).date()
    return now  # already a date


def _meta_timestamp_on_jst_date(raw: str | None, *, day: date) -> bool:
    if not raw:
        return False
    dt = _parse_iso_utc(raw)
    if dt is None:
        # when it is only YYYY-MM-DD
        try:
            return date.fromisoformat(raw.strip()[:10]) == day
        except ValueError:
            return False
    return dt.astimezone(_tz()).date() == day


def _review_detail_from_stories(stories: list[dict[str, Any]]) -> str:
    """Return the detail line of the latest PIPELINE_STATUS: REVIEW comment."""
    latest = ""
    for story in stories:
        text = story.get("text") or ""
        if "PIPELINE_STATUS: REVIEW" not in text:
            continue
        before, _, _ = text.partition("PIPELINE_STATUS: REVIEW")
        detail = before.strip()
        if detail:
            latest = detail
    return latest


def build_review_request_reasons(
    sections: dict[str, str],
    *,
    body: str,
    stories: list[dict[str, Any]] | None = None,
    sections_config: Any = None,
) -> list[str]:
    """Bullet list of REVIEW reasons (deterministic): violations -> judgments -> drift detail."""
    violations = check_design_doc(
        sections, body=body,
        sections_config=sections_config,
    )
    if violations:
        def _violation_label(v: Any) -> str:
            label = language().violations.get(v.rule_id)
            if label:
                return f"{label}: {v.message}"
            return f"{v.rule_id}: {v.message}"

        reasons = [_violation_label(v) for v in violations]
        # for section_missing, add the lane list
        if any(v.rule_id == "design_doc.section_missing" for v in violations):
            if sections_config is not None:
                names = sections_config.non_default_section_names()
                if names:
                    reasons.append(language().messages["review_request.lane_prompt"] + " / ".join(names))
        return reasons
    judgment = _judgment_text(sections).strip()
    if judgment:
        return [language().messages["review_request.judgment_needed"].format(judgment=judgment)]
    drift = _review_detail_from_stories(stories or [])
    if drift:
        return [drift]
    return [language().messages["review_request.reason_unknown"]]


def build_review_request_text(
    *,
    task_name: str,
    permalink: str | None,
    reasons: Sequence[str],
) -> str:
    """Fixed template of the REVIEW exit request."""
    lines = [
        language().messages["review_request.title"].format(task_name=task_name),
    ]
    if permalink:
        lines.append(permalink)
    lines.extend(["", language().messages["review_request.missing_heading"]])
    for reason in reasons:
        lines.append(f"- {reason}")
    lines.extend(["", language().messages["review_request.reply_hint"]])
    return "\n".join(lines)


def request_review(
    task_gid: str,
    *,
    project_gid: str | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
    event_sink: EventSink | None = None,
) -> RequestReviewResult:
    """Request a REVIEW node over Slack, register it in pending and write `_review_requested`. The state stays REVIEW."""
    from tasksmith import pending as loops_pending

    ch = settings().request_channel_id
    task = task_store().get_task(
        task_gid,
    )
    body = task.get("body")
    if not isinstance(body, str) or not is_controlled_notes(body):
        raise ValueError("Body is not in the fixed format")
    sections = parse_sections(body)
    if sections is None:
        raise ValueError("Body is not in the fixed format")
    state = parse_meta_field(sections, "state")
    if state != PipelineState.REVIEW.value:
        return RequestReviewResult(
            task_gid=task_gid,
            channel_id=ch if isinstance(ch, str) else None,
            thread_ts=None,
            request_text="",
            dry_run=dry_run,
            skipped=True,
            skip_reason=language().messages["request_review.skip.not_review"].format(state=state),
        )
    owner = parse_meta_field(sections, "owner")
    if owner not in (OwnerType.AI.value, OwnerType.HUMAN.value):
        return RequestReviewResult(
            task_gid=task_gid,
            channel_id=ch if isinstance(ch, str) else None,
            thread_ts=None,
            request_text="",
            dry_run=dry_run,
            skipped=True,
            skip_reason=language().messages["request_review.skip.owner_invalid"].format(owner=owner),
        )
    if parse_meta_field(sections, "_review_requested"):
        return RequestReviewResult(
            task_gid=task_gid,
            channel_id=ch if isinstance(ch, str) else None,
            thread_ts=None,
            request_text="",
            dry_run=dry_run,
            skipped=True,
            skip_reason=language().messages["request_review.skip.already_requested"],
        )

    stories = task_store().get_task_stories(task_gid)
    _req_sections_cfg = _load_sections_config()
    reasons = build_review_request_reasons(
        sections, body=body, stories=stories,
        sections_config=_req_sections_cfg,
    )
    task_name = str(task.get("name") or task_gid)
    asana_permalink = task.get("permalink_url") if isinstance(task.get("permalink_url"), str) else None
    request_text = build_review_request_text(
        task_name=task_name, permalink=asana_permalink, reasons=reasons
    )
    now_iso = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    listing = "\n".join(f"- {r}" for r in reasons)

    if dry_run:
        return RequestReviewResult(
            task_gid=task_gid,
            channel_id=ch if isinstance(ch, str) else None,
            thread_ts=None,
            request_text=request_text,
            dry_run=True,
        )

    if not ch:
        raise ValueError(
            "request_channel_id is not set (tasksmith or asana_conversation.channel_ids in configs/secretary.yaml)"
        )

    if not human_channel_installed():
        raise RuntimeError("human channel is not installed")

    notes_with_meta = update_meta_field(
        body, "_review_requested", now_iso, create=True
    )
    task_store().update_task(task_gid, body=notes_with_meta)

    exec_id = _execution_id("request-review", task_gid)
    ref = human_channel().ask(request_text)
    loops_pending.register(ref, f"tasksmith:{task_gid}")
    loops_pending.set_question(ref, task_gid)
    channel_id, _, thread_ts = ref.partition(":")
    _emit_event(
        event_sink,
        "review_requested",
        task_gid=task_gid,
        task_name=task.get("name"),
        permalink_url=asana_permalink,
        summary=(
            language().messages["events.request_review.summary"].format(channel=ch, reason=first_line(reasons[0] if reasons else ''))
        ),
        execution_id=exec_id,
        actor="tasksmith-request-review",
    )

    slack_permalink = human_channel().link(ref) or ""
    detail = listing
    if slack_permalink:
        detail = language().messages["request_review.detail_with_link"].format(listing=listing, permalink=slack_permalink)
    task_store().add_comment(
        task_gid,
        pipeline_status_comment(
            state=PipelineState.REVIEW,
            actor="tasksmith-request-review",
            execution_id=exec_id,
            detail=detail,
        ),
    )
    return RequestReviewResult(
        task_gid=task_gid,
        channel_id=ch,
        thread_ts=thread_ts,
        request_text=request_text,
        dry_run=False,
    )


def _cap_day_from_now(now: datetime, quiet_hours: list[str] | None = None) -> date:
    """"Today" for the cap count: computed with 08:00 JST as the day boundary (uses quiet_hours[1])."""
    now_jst = now.astimezone(_tz())
    boundary_hour = 8
    boundary_min = 0
    if quiet_hours and len(quiet_hours) == 2:
        try:
            bh, bm = _parse_hhmm(quiet_hours[1])
            boundary_hour = bh
            boundary_min = bm
        except ValueError:
            pass
    cur_min = now_jst.hour * 60 + now_jst.minute
    bnd_min = boundary_hour * 60 + boundary_min
    if cur_min < bnd_min:
        return (now_jst - timedelta(days=1)).date()
    return now_jst.date()


def _count_review_requested_today(
    review_nodes: list[ReconcileNode],
    *,
    day: date,
) -> int:
    count = 0
    for node in review_nodes:
        task = task_store().get_task(node.task_gid)
        body = task.get("body")
        if not isinstance(body, str):
            continue
        secs = parse_sections(body)
        if secs is None:
            continue
        if _meta_timestamp_on_jst_date(parse_meta_field(secs, "_review_requested"), day=day):
            count += 1
    return count


def _request_review_batch(
    review_nodes: list[ReconcileNode],
    *,
    project_gid: str | None,
    max_review: int,
    dry_run: bool,
    now: datetime | None = None,
    event_sink: EventSink | None = None,
    skip_gids: frozenset[str] | set[str] = frozenset(),
    cfg: Mapping[str, Any] | None = None,
) -> list[str]:
    """request_review from the REVIEW list within the cap. Return a list of strings for messages.

    Nodes in skip_gids (e.g. made READY by elaborate in the same tick) are excluded from both the targets and the cap count.
    """
    del cfg  # legacy callers; caps/channel come from settings()
    messages: list[str] = []
    if max_review < 0:
        raise ValueError("--max-review must be 0 or greater")
    now_dt = now or datetime.now(_tz())
    st = settings()
    quiet_hours = list(st.quiet_hours) if st.quiet_hours else None
    dag_nodes = [
        n for n in review_nodes if n.flow_type == "dag" and n.task_gid not in skip_gids
    ]
    day = _cap_day_from_now(now_dt, quiet_hours)
    daily_cap = int(st.review_daily_cap)
    already_today = _count_review_requested_today(dag_nodes, day=day)
    remaining_daily = max(0, daily_cap - already_today)
    budget = min(max_review, remaining_daily)
    if budget <= 0:
        if dag_nodes:
            if max_review == 0:
                messages.append("skip review: --max-review=0")
            elif remaining_daily <= 0:
                messages.append(
                    language().messages["request_review.batch.skip_cap"].format(daily_cap=daily_cap, already_today=already_today)
                )
            else:
                messages.append(f"skip review: budget=0 (max_review={max_review})")
        return messages

    posted = 0
    for node in dag_nodes:
        if posted >= budget:
            messages.append(
                language().messages["request_review.batch.skip_remaining"].format(task_gid=node.task_gid)
            )
            continue
        owner = node.owner_type
        if owner not in (OwnerType.AI.value, OwnerType.HUMAN.value):
            messages.append(f"skip review: gid={node.task_gid} owner={owner}")
            continue
        try:
            result = request_review(
                node.task_gid,
                project_gid=project_gid,
                dry_run=dry_run,
                now=now,
                event_sink=event_sink,
            )
        except Exception as exc:
            messages.append(f"skip review: gid={node.task_gid} error={type(exc).__name__}: {exc}")
            continue
        if result.skipped:
            messages.append(f"skip review: gid={node.task_gid} {result.skip_reason}")
            continue
        posted += 1
        if dry_run:
            messages.append(f"dry-run requested_review gid={node.task_gid}")
        else:
            messages.append(
                f"requested_review gid={result.task_gid} channel={result.channel_id} ts={result.thread_ts}"
            )
    return messages


def _digest_state_path() -> Path:
    return settings().state_dir / _DIGEST_STATE_NAME


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        delete=False,
        suffix=".tmp",
    ) as tf:
        json.dump(payload, tf, ensure_ascii=False, indent=2, sort_keys=True)
        tf.write("\n")
        tmp_name = tf.name
    os.replace(tmp_name, path)


def _load_digest_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _review_stale_days(sections: dict[str, str] | None, *, now: datetime) -> int:
    if not sections:
        return 0
    raw = parse_meta_field(sections, "_review_requested")
    if not raw:
        return 0
    dt = _parse_iso_utc(raw)
    if dt is None:
        try:
            d = date.fromisoformat(raw.strip()[:10])
        except ValueError:
            return 0
        return max(0, (_jst_today(now) - d).days)
    return max(0, (_jst_today(now) - dt.astimezone(_tz()).date()).days)


def post_review_digest(
    *,
    review: list[ReconcileNode],
    overdue: list[ReconcileNode],
    dry_run: bool = False,
    digest_state_path: Path | None = None,
    now: datetime | None = None,
    inverted_periods: list[ReconcileNode] | None = None,
    cfg: Mapping[str, Any] | None = None,
) -> list[str]:
    """Daily digest of REVIEW / overdue / inverted periods. Posted once per JST date, at or after digest_at."""
    del cfg  # legacy callers; channel/digest_at come from settings()
    messages: list[str] = []
    now_dt = now or datetime.now(_tz())
    today = _jst_today(now_dt)
    # personal tasks are excluded from the REVIEW digest (inverted periods are not narrowed by flow)
    dag_review = [n for n in review if n.flow_type == "dag"]
    inverted = list(inverted_periods or [])
    if not dag_review and not overdue and not inverted:
        return messages
    state_path = digest_state_path or _digest_state_path()
    state = _load_digest_state(state_path)
    last = state.get("last_posted_date")
    if isinstance(last, str) and last == today.isoformat():
        messages.append("skip digest: already posted today")
        return messages

    # when digest_at is set, post only on the first tick at or after the given time
    digest_at = settings().digest_at
    if digest_at:
        try:
            dh, dm = _parse_hhmm(digest_at)
            now_jst = now_dt.astimezone(_tz())
            cur_min = now_jst.hour * 60 + now_jst.minute
            digest_min = dh * 60 + dm
            if cur_min < digest_min:
                messages.append(language().messages["digest.skip_before"].format(digest_at=digest_at))
                return messages
        except ValueError:
            pass

    channel_id = settings().request_channel_id
    if not (isinstance(channel_id, str) and channel_id.strip()):
        messages.append(language().messages["digest.skip_no_channel"])
        return messages

    max_stale = 0
    lines = [
        language().messages["digest.title"].format(date=today.isoformat()),
        "",
        language().messages["digest.review_count"].format(count=len(dag_review)),
        language().messages["digest.overdue_count"].format(count=len(overdue)),
        language().messages["digest.inverted_count"].format(count=len(inverted)),
        "",
    ]
    if dag_review:
        lines.append("## REVIEW")
        for node in dag_review:
            task = task_store().get_task(node.task_gid)
            body = task.get("body")
            secs = parse_sections(body) if isinstance(body, str) else None
            stale = _review_stale_days(secs, now=now_dt)
            max_stale = max(max_stale, stale)
            link = node.permalink_url or (
                task.get("permalink_url") if isinstance(task.get("permalink_url"), str) else None
            )
            entry = f"- {node.name} ({node.task_gid})"
            if link:
                entry += f" {link}"
            if stale:
                entry += language().messages["digest.stale_suffix"].format(stale=stale)
            lines.append(entry)
        lines.append("")
    lines.insert(3, language().messages["digest.max_stale"].format(max_stale=max_stale))
    if overdue:
        lines.append("## overdue")
        for node in overdue:
            link = node.permalink_url or ""
            entry = f"- {node.name} ({node.task_gid})"
            if link:
                entry += f" {link}"
            lines.append(entry)
        lines.append("")
    if inverted:
        lines.append(language().messages["digest.inverted_heading"])
        for node in inverted:
            start_on = parse_start_on(node.body)
            due_s = node.due_on or "?"
            start_s = start_on or "?"
            entry = (
                language().messages["digest.inverted_item"].format(name=node.name, start_s=start_s, due_s=due_s)
            )
            if node.permalink_url:
                entry += f" {node.permalink_url}"
            lines.append(entry)
    text = "\n".join(lines).strip()

    if dry_run:
        messages.append("dry-run digest posted")
        return messages

    if not human_channel_installed():
        messages.append("skip digest: human channel not installed")
        return messages
    try:
        human_channel().announce(text)
    except Exception:
        messages.append("skip digest: announce failed")
        return messages
    _atomic_write_json(state_path, {"last_posted_date": today.isoformat()})
    messages.append("digest posted")
    return messages


def collect_human_reply_violations(
    payload: dict[str, Any] | None,
    *,
    choices: list[str],
    today: date | None = None,
) -> list[str]:
    """Deterministic verification of record_human_reply."""
    if payload is None:
        return [language().violations["human_reply.json_missing"]]
    v: list[str] = []
    decision = payload.get("decision")
    if decision not in _HUMAN_DECISIONS:
        v.append(
            language().violations["human_reply.decision_invalid"]
        )
    choice = payload.get("choice")
    if choice is not None and choice != "":
        if not isinstance(choice, str):
            v.append(language().violations["human_reply.choice_not_string"])
        elif choices and choice.upper() not in {c.upper() for c in choices}:
            v.append(language().violations["human_reply.choice_not_in_options"].format(choice=choice, choices=choices))
    due_on = payload.get("due_on")
    if decision in ("defer", "replan") and (due_on is None or due_on == ""):
        v.append(language().violations["human_reply.due_on_format"])
    elif due_on is not None and due_on != "":
        if not isinstance(due_on, str) or not _DATE_RE.match(due_on):
            v.append(language().violations["human_reply.due_on_format"])
        else:
            base = today or date.today()
            try:
                d = date.fromisoformat(due_on)
            except ValueError:
                v.append(language().violations["human_reply.due_on_format"])
            else:
                if d <= base:
                    v.append(language().violations["human_reply.due_on_future"])
    if decision == "replan":
        cascade = payload.get("cascade")
        if not isinstance(cascade, bool):
            v.append(language().violations["human_reply.cascade_bool"])
        if "split" in payload and not isinstance(payload.get("split"), list):
            v.append(language().violations["human_reply.split_array"])
    note = payload.get("note")
    if note is not None and not isinstance(note, str):
        v.append(language().violations["human_reply.note_not_string"])
    return v


def _build_human_reply_prompt(*, message: str, sections: dict[str, str], choices: list[str]) -> str:
    objective = _objective_text(sections).strip()
    draft = (sections.get("draft") or "").strip()
    acceptance = "\n".join(section_items(sections.get("acceptance") or ""))
    judgment = _judgment_text(sections)
    none_text = language().prompts["human_reply.none"]
    choice_hint = ", ".join(choices) if choices else none_text
    return (
        language().prompts["human_reply.prompt"].format(choice_hint=choice_hint, objective=objective, judgment=judgment or none_text, draft=draft, acceptance=acceptance, message=message)
    )


def _replace_judgment_with_choice(objective_text: str, choice: str | None, note: str | None) -> str:
    """Replace the judgments block with the decision."""
    plain = objective_text or ""
    decision_text = language().messages["judgment_replace.decided"].format(choice=choice) if choice else language().messages["judgment_replace.decided_plain"]
    if note:
        decision_text += language().messages["common.note_suffix"].format(note=note)
    replacement = f"### {language().headings['judgments']}\n{decision_text}"
    if _judgment_heading_re().search(plain):
        new_plain = _judgment_heading_re().sub(replacement, plain, count=1)
    elif re.search(rf"###\s*{_judgment_heading()}", plain):
        new_plain = re.sub(rf"###\s*{_judgment_heading()}[\s\S]*", replacement, plain, count=1)
    else:
        new_plain = plain
    return new_plain


def _append_draft(draft: str, addition: str) -> str:
    addition = (addition or "").strip()
    if not addition:
        return draft
    draft = (draft or "").strip()
    return f"{draft}\n{addition}".strip() if draft else addition


def record_human_reply(
    task_gid: str,
    message: str,
    *,
    ref: str,
    project_gid: str | None = None,
    dry_run: bool = False,
    today: date | None = None,
    metrics_path: Path | None = None,
    event_sink: EventSink | None = None,
) -> RecordHumanReplyResult:
    """Interpret the human's Slack message, then transition the state, update the body and clean up pending."""
    from tasksmith import pending as loops_pending

    exec_id = _execution_id("human-reply", task_gid)

    def _finish(
        result: RecordHumanReplyResult,
        *,
        interpretation: dict[str, Any] | None,
    ) -> RecordHumanReplyResult:
        if not dry_run:
            _emit_event(
                event_sink,
                "human_reply",
                task_gid=task_gid,
                task_name=task.get("name"),
                permalink_url=task.get("permalink_url"),
                summary=(
                    language().messages["events.record_human_reply.summary"].format(reply=message.strip()[:80], decision=result.decision, transitioned_to=result.transitioned_to)
                ),
                execution_id=exec_id,
                actor="tasksmith-record-human-reply",
            )
        _append_metrics(
            {
                "kind": "tasksmith_human_reply",
                "timestamp": _utc_now_iso(),
                "task_gid": task_gid,
                "raw_message": message,
                "interpretation": interpretation,
                "decision": result.decision,
                "violations": list(result.violations),
                "transitioned_to": result.transitioned_to,
            },
            metrics_path=metrics_path,
        )
        return result

    task = task_store().get_task(
        task_gid,
    )
    body = task.get("body")
    if not isinstance(body, str) or not is_controlled_notes(body):
        raise ValueError("Body is not in the fixed format")
    sections = parse_sections(body)
    if sections is None:
        raise ValueError("Body is not in the fixed format")

    choices = parse_judgment_choices(_judgment_text(sections) or _objective_text(sections))
    prompt = _build_human_reply_prompt(message=message, sections=sections, choices=choices)

    engine_err = ""
    try:
        engine_stdout = thinker().ask(prompt, weight="light", timeout=600)
    except EngineUnavailable:
        if not dry_run and human_channel_installed():
            human_channel().follow_up(ref, language().messages["human_reply.engine_paused"])
        raise
    except (EngineFailed, TimeoutError) as exc:
        engine_stdout = ""
        engine_err = str(exc)
    except Exception as exc:  # noqa: BLE001 - normalize a thinker failure into an empty reply
        engine_stdout = ""
        engine_err = str(exc)

    stdout = engine_stdout
    if engine_err and not stdout:
        stdout = engine_err

    payload = _extract_payload(stdout)
    violations = collect_human_reply_violations(payload, choices=choices, today=today)
    pending_entry = loops_pending.lookup(ref)
    can_clarify = pending_entry is not None and pending_entry.question_id is not None

    def _comment(detail: str, state: PipelineState) -> None:
        if dry_run:
            return
        task_store().add_comment(
            task_gid,
            pipeline_status_comment(
                state=state,
                actor="tasksmith-record-human-reply",
                execution_id=exec_id,
                detail=detail,
            ),
        )

    def _post_clarify(text: str) -> None:
        if dry_run:
            return
        if human_channel_installed():
            human_channel().follow_up(ref, text)

    if violations:
        clarify = (
            language().messages["human_reply.clarify_unresolved"].format(reasons='; '.join(violations))
        )
        if can_clarify:
            _post_clarify(clarify)
            if not dry_run:
                loops_pending.clear_question(ref)
        if not dry_run:
            transition_task(
                task_gid,
                state=PipelineState.REVIEW,
                actor="tasksmith-record-human-reply",
                execution_id=exec_id,
                detail=language().messages["human_reply.verify_failed_detail"].format(message=message, interpretation=stdout[:2000], violations=violations),
            )
        return _finish(
            RecordHumanReplyResult(
                task_gid=task_gid,
                decision="unclear",
                choice=None,
                note=None,
                transitioned_to=PipelineState.REVIEW.value,
                violations=violations,
            ),
            interpretation=payload if isinstance(payload, dict) else None,
        )

    assert payload is not None
    decision = str(payload["decision"])
    choice_raw = payload.get("choice")
    choice = str(choice_raw).strip() if isinstance(choice_raw, str) and choice_raw.strip() else None
    note_raw = payload.get("note")
    note = str(note_raw).strip() if isinstance(note_raw, str) and note_raw.strip() else None
    due_on_raw = payload.get("due_on")
    due_on = (
        str(due_on_raw).strip()
        if isinstance(due_on_raw, str) and due_on_raw.strip()
        else None
    )

    interpretation = json.dumps(payload, ensure_ascii=False)
    audit = language().messages["human_reply.interpretation_audit"].format(message=message, interpretation=interpretation)

    if decision == "unclear":
        if can_clarify:
            _post_clarify(
                language().messages["human_reply.clarify_unclear"]
            )
            if not dry_run:
                loops_pending.clear_question(ref)
        if not dry_run:
            transition_task(
                task_gid,
                state=PipelineState.REVIEW,
                actor="tasksmith-record-human-reply",
                execution_id=exec_id,
                detail=audit,
            )
        # unclear keeps pending (no unregister); the question is cleared when the clarification is sent
        return _finish(
            RecordHumanReplyResult(
                task_gid=task_gid,
                decision=decision,
                choice=choice,
                note=note,
                transitioned_to=PipelineState.REVIEW.value,
                violations=[],
            ),
            interpretation=payload,
        )

    def _apply_fill_notes(base_notes: str) -> str:
        """Apply only the non-empty fields of the fill payload to the body."""
        updated = base_notes
        executor_raw = payload.get("executor")
        if isinstance(executor_raw, str) and executor_raw.strip():
            updated = update_meta_field(updated, "executor", executor_raw.strip())
        deliverables_raw = payload.get("deliverables")
        if isinstance(deliverables_raw, list):
            items = [str(x).strip() for x in deliverables_raw if str(x).strip()]
            if items:
                updated = replace_editable_sections(updated, deliverables=items)
        elif isinstance(deliverables_raw, str) and deliverables_raw.strip():
            updated = replace_editable_sections(updated, deliverables=deliverables_raw.strip())
        acceptance_raw = payload.get("acceptance")
        if isinstance(acceptance_raw, list):
            items = [str(x).strip() for x in acceptance_raw if str(x).strip()]
            if items:
                updated = replace_editable_sections(updated, acceptance=items)
        elif isinstance(acceptance_raw, str) and acceptance_raw.strip():
            updated = replace_editable_sections(updated, acceptance=acceptance_raw.strip())

        judgment_choice_raw = payload.get("judgment_choice")
        jchoice = (
            str(judgment_choice_raw).strip()
            if isinstance(judgment_choice_raw, str) and judgment_choice_raw.strip()
            else None
        )
        # choice fallback when judgment_choice is not given (the LLM returned only choice)
        if jchoice is None and choice:
            jchoice = choice

        secs = parse_sections(updated)
        assert secs is not None
        draft_cur = secs.get("draft") or ""
        is_v2_local = "judgments" in secs
        if jchoice:
            # move judgments to draft, empty the section and let it pass the READY gate
            decision_line = language().messages["judgment_replace.decided"].format(choice=jchoice)
            if note:
                decision_line += language().messages["common.note_suffix"].format(note=note)
            draft_cur = _append_draft(draft_cur, decision_line)
            if is_v2_local:
                secs["judgments"] = ""
                secs["draft"] = draft_cur
                updated = serialize_sections(secs)
            else:
                new_objective = _replace_judgment_with_choice(
                    _objective_text(secs), jchoice, note
                )
                secs["objective"] = new_objective
                secs["draft"] = draft_cur
                updated = serialize_sections(secs)
        elif note:
            draft_cur = _append_draft(draft_cur, note)
            updated = replace_editable_sections(updated, draft=draft_cur)
        return updated

    if decision == "fill":
        filled_notes = _apply_fill_notes(body)
        filled_sections = parse_sections(filled_notes)
        assert filled_sections is not None

        design_violations = check_design_doc(
            filled_sections, body=filled_notes
        )
        if dry_run:
            target = (
                PipelineState.READY.value
                if not design_violations
                else PipelineState.REVIEW.value
            )
            return _finish(
                RecordHumanReplyResult(
                    task_gid=task_gid,
                    decision=decision,
                    choice=choice,
                    note=note,
                    transitioned_to=target,
                    violations=[f"{v.rule_id}: {v.message}" for v in design_violations],
                ),
                interpretation=payload,
            )
        task_store().update_task(task_gid, body=filled_notes)
        if not design_violations:
            transition_task(
                task_gid,
                state=PipelineState.READY,
                actor="tasksmith-fill",
                execution_id=_execution_id("human-reply-fill", task_gid),
                detail=audit,
            )
            loops_pending.clear_question(ref)
            loops_pending.unregister(ref)
            return _finish(
                RecordHumanReplyResult(
                    task_gid=task_gid,
                    decision=decision,
                    choice=choice,
                    note=note,
                    transitioned_to=PipelineState.READY.value,
                ),
                interpretation=payload,
            )
        # remaining violations -> stay in REVIEW, ask back only once
        listing = format_violations_comment(design_violations)
        clarify = (
            language().messages["human_reply.fill_incomplete"] + listing
        )
        if can_clarify:
            _post_clarify(clarify)
            loops_pending.clear_question(ref)
        _comment(audit + language().messages["human_reply.fill_remaining"].format(listing=listing), PipelineState.REVIEW)
        return _finish(
            RecordHumanReplyResult(
                task_gid=task_gid,
                decision=decision,
                choice=choice,
                note=note,
                transitioned_to=PipelineState.REVIEW.value,
                violations=[f"{v.rule_id}: {v.message}" for v in design_violations],
            ),
            interpretation=payload,
        )

    if dry_run:
        target_map = {
            "approve": PipelineState.DONE.value,
            "done": PipelineState.DONE.value,
            "revise": PipelineState.READY.value,
            "reject": PipelineState.BLOCKED.value,
            "defer": PipelineState.WAITING_HUMAN.value,
            "replan": PipelineState.WAITING_HUMAN.value,
        }
        if decision in ("defer", "replan"):
            old_due = task.get("due_on")
            delta_days: int | None = None
            if (
                isinstance(old_due, str)
                and _DATE_RE.match(old_due)
                and due_on
                and _DATE_RE.match(due_on)
            ):
                delta_days = (date.fromisoformat(due_on) - date.fromisoformat(old_due)).days
            split_raw = payload.get("split") if decision == "replan" else []
            if not isinstance(split_raw, list):
                split_raw = []
            count_raw = parse_meta_field(sections, "_replan_count")
            replan_n = int(count_raw) if count_raw and str(count_raw).isdigit() else 0
            _append_metrics(
                {
                    "kind": "tasksmith_replan",
                    "timestamp": _utc_now_iso(),
                    "task_gid": task_gid,
                    "delta_days": delta_days,
                    "cascaded": [],
                    "split_count": len([s for s in split_raw if str(s).strip()]),
                    "replan_count": replan_n + 1,
                },
                metrics_path=metrics_path,
            )
        return _finish(
            RecordHumanReplyResult(
                task_gid=task_gid,
                decision=decision,
                choice=choice,
                note=note,
                transitioned_to=target_map[decision],
                violations=[],
            ),
            interpretation=payload,
        )

    draft = sections.get("draft") or ""
    objective_text = _objective_text(sections)
    updated_notes = body
    is_v2 = "judgments" in sections

    if decision in ("approve", "done"):
        draft_add = language().messages["human_reply.decision_draft_line"].format(decision_label=choice or decision)
        if note:
            draft_add += f" / {note}"
        draft = _append_draft(draft, draft_add)
        secs = dict(sections)
        if is_v2:
            secs["judgments"] = ""
            secs["draft"] = draft
        else:
            new_objective = _replace_judgment_with_choice(objective_text, choice, note)
            secs["objective"] = new_objective
            secs["draft"] = draft
        updated_notes = serialize_sections(secs)
        task_store().update_task(task_gid, body=updated_notes)
        transition_task(
            task_gid,
            state=PipelineState.DONE,
            actor="tasksmith-record-human-reply",
            execution_id=exec_id,
            detail=audit,
        )
        loops_pending.clear_question(ref)
        loops_pending.unregister(ref)
        # andon answer: approve/done → resume
        _request_id_val = _meta_request_id(sections)
        if _request_id_val:
            task_store().add_comment(task_gid, f"request {_request_id_val} answered: resume")
        return _finish(
            RecordHumanReplyResult(
                task_gid=task_gid,
                decision=decision,
                choice=choice,
                note=note,
                transitioned_to=PipelineState.DONE.value,
            ),
            interpretation=payload,
        )

    if decision == "revise":
        draft = _append_draft(draft, note or language().messages["human_reply.revise_note"])
        updated_notes = replace_editable_sections(body, draft=draft)
        task_store().update_task(task_gid, body=updated_notes)
        transition_task(
            task_gid,
            state=PipelineState.READY,
            actor="tasksmith-record-human-reply",
            execution_id=exec_id,
            detail=audit,
        )
        loops_pending.clear_question(ref)
        loops_pending.unregister(ref)
        return _finish(
            RecordHumanReplyResult(
                task_gid=task_gid,
                decision=decision,
                choice=choice,
                note=note,
                transitioned_to=PipelineState.READY.value,
            ),
            interpretation=payload,
        )

    if decision == "reject":
        draft = _append_draft(draft, note or language().messages["human_reply.reject_note"])
        updated_notes = replace_editable_sections(body, draft=draft)
        task_store().update_task(task_gid, body=updated_notes)
        transition_task(
            task_gid,
            state=PipelineState.BLOCKED,
            actor="tasksmith-record-human-reply",
            execution_id=exec_id,
            detail=audit,
        )
        loops_pending.clear_question(ref)
        loops_pending.unregister(ref)
        # andon answer: reject → abandon
        _request_id_val = _meta_request_id(sections)
        if _request_id_val:
            task_store().add_comment(task_gid, f"request {_request_id_val} answered: abandon")
        return _finish(
            RecordHumanReplyResult(
                task_gid=task_gid,
                decision=decision,
                choice=choice,
                note=note,
                transitioned_to=PipelineState.BLOCKED.value,
            ),
            interpretation=payload,
        )

    # defer / replan (defer is a compatibility wrapper for cascade=False, split=[])
    assert decision in ("defer", "replan")
    assert due_on is not None
    cascade = False if decision == "defer" else bool(payload.get("cascade"))
    split_titles: list[str] = []
    if decision == "replan":
        split_raw = payload.get("split")
        if isinstance(split_raw, list):
            split_titles = [str(s).strip() for s in split_raw if str(s).strip()]

    old_due_raw = task.get("due_on")
    old_due = (
        str(old_due_raw).strip()
        if isinstance(old_due_raw, str) and _DATE_RE.match(str(old_due_raw).strip())
        else None
    )
    delta_days: int | None = None
    if old_due is not None:
        delta_days = (date.fromisoformat(due_on) - date.fromisoformat(old_due)).days

    count_raw = parse_meta_field(sections, "_replan_count")
    replan_count = int(count_raw) if count_raw and str(count_raw).isdigit() else 0
    replan_count += 1
    now_iso = _utc_now_iso()
    updated_notes = update_meta_field(body, "_replan_count", str(replan_count), create=True)
    updated_notes = update_meta_field(updated_notes, "_waiting_since", now_iso, create=True)
    updated_notes = update_meta_field(updated_notes, "_reminded_count", "0", create=True)
    task_store().update_task(task_gid, due_on=due_on, body=updated_notes)

    cascaded: list[str] = []
    if cascade and delta_days is not None:
        pid = _project_gid(project_gid)
        rows = task_store().list_tasks(pid)
        all_by_gid = _index_all_tasks(rows)
        new_due_d = date.fromisoformat(due_on)
        for gid, row in all_by_gid.items():
            if gid == task_gid:
                continue
            notes_s = row.get("body")
            deps = _body_depends_on(notes_s if isinstance(notes_s, str) else None)
            if task_gid not in deps:
                continue
            state, _ = _body_meta(notes_s if isinstance(notes_s, str) else None)
            if state in (PipelineState.DONE.value, PipelineState.BLOCKED.value):
                continue
            row_due = row.get("due_on")
            if not isinstance(row_due, str) or not _DATE_RE.match(row_due):
                continue
            row_due_d = date.fromisoformat(row_due)
            if row_due_d >= new_due_d:
                continue
            shifted = (row_due_d + timedelta(days=delta_days)).isoformat()
            task_store().update_task(gid, due_on=shifted)
            task_store().add_comment(
                gid,
                pipeline_status_comment(
                    state=PipelineState.WAITING_HUMAN,
                    actor="tasksmith-replan",
                    execution_id=_execution_id("replan-cascade", gid),
                    detail=f"cascade from {task_gid}: due_on {row_due} → {shifted}",
                ),
            )
            cascaded.append(gid)

    for title in split_titles:
        sub_notes = build_controlled_notes(
            objective=title, owner=OwnerType.HUMAN, state=PipelineState.REVIEW
        )
        task_store().create_subtask(
            task_gid, title, due_on=due_on, body=sub_notes,
            assign_owner=OwnerType.HUMAN.value,
        )

    detail = audit + f"\ndue_on → {due_on}"
    if cascaded:
        detail += f"\ncascaded: {', '.join(cascaded)}"
    if split_titles:
        detail += f"\nsplit: {len(split_titles)}"
    task_store().add_comment(
        task_gid,
        pipeline_status_comment(
            state=PipelineState.WAITING_HUMAN,
            actor="tasksmith-replan",
            execution_id=_execution_id("replan", task_gid),
            detail=detail,
        ),
    )
    loops_pending.clear_question(ref)
    loops_pending.set_question(ref, task_gid)
    _append_metrics(
        {
            "kind": "tasksmith_replan",
            "timestamp": now_iso,
            "task_gid": task_gid,
            "delta_days": delta_days,
            "cascaded": cascaded,
            "split_count": len(split_titles),
            "replan_count": replan_count,
        },
        metrics_path=metrics_path,
    )
    return _finish(
        RecordHumanReplyResult(
            task_gid=task_gid,
            decision=decision,
            choice=choice,
            note=note,
            transitioned_to=PipelineState.WAITING_HUMAN.value,
        ),
        interpretation=payload,
    )


def _next_monday(day: date) -> date:
    """The first Monday after day (the following week if day is a Monday)."""
    days_ahead = 0 - day.weekday()  # Monday=0
    if days_ahead <= 0:
        days_ahead += 7
    return day + timedelta(days=days_ahead)


def _parse_reminded_count(sections: dict[str, str] | None) -> int:
    if not sections:
        return 0
    raw = parse_meta_field(sections, "_reminded_count")
    if raw is None or not str(raw).strip():
        return 0
    try:
        return max(0, int(str(raw).strip()))
    except ValueError:
        return 0


def _remind_threshold_hours(remind_after_hours: int, reminded_count: int) -> int:
    """Cumulative interval: remind_after_hours * (2^(count+1) - 1)."""
    return int(remind_after_hours) * ((2 ** (reminded_count + 1)) - 1)


def _parse_iso_utc(value: str) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def remind_waiting_humans(
    waiting: list[ReconcileNode],
    *,
    remind_after_hours: int,
    remind_max_count: int = 3,
    dry_run: bool = False,
    now: datetime | None = None,
    project_gid: str | None = None,
) -> list[str]:
    """Remind overdue WAITING_HUMAN nodes with choices, or move them to REVIEW when the cap is exceeded."""
    from tasksmith import pending as loops_pending

    acted: list[str] = []
    now_utc = now or datetime.now(timezone.utc)
    for node in waiting:
        task = task_store().get_task(
            node.task_gid
        )
        body = task.get("body")
        notes_s = body if isinstance(body, str) else None
        secs = parse_sections(notes_s) if notes_s else None
        if secs and parse_meta_field(secs, "_remind") == "off":
            continue
        since_raw = parse_meta_field(secs, "_waiting_since") if secs else None
        if not since_raw:
            continue
        since_dt = _parse_iso_utc(since_raw)
        if since_dt is None:
            continue
        count = _parse_reminded_count(secs)
        elapsed = now_utc - since_dt
        threshold = timedelta(hours=_remind_threshold_hours(remind_after_hours, count))
        if elapsed < threshold:
            continue
        ref = loops_pending.find_by_loop_id(f"tasksmith:{node.task_gid}")
        if ref is None:
            continue

        if count >= remind_max_count:
            escalate_text = (
                language().messages["remind.escalated"].format(name=node.name)
            )
            if not dry_run:
                if human_channel_installed():
                    human_channel().follow_up(ref, escalate_text)
                transition_task(
                    node.task_gid,
                    state=PipelineState.REVIEW,
                    actor="tasksmith-remind-escalate",
                    execution_id=_execution_id("remind-escalate", node.task_gid),
                    detail=escalate_text,
                )
                loops_pending.unregister(ref)
            acted.append(node.task_gid)
            continue

        due_on_raw = task.get("due_on")
        due_on = (
            str(due_on_raw).strip()
            if isinstance(due_on_raw, str) and _DATE_RE.match(str(due_on_raw).strip())
            else None
        )
        lines = [
            language().messages["remind.waiting"].format(name=node.name),
            language().messages["remind.option_a"],
        ]
        if due_on is not None:
            due_d = date.fromisoformat(due_on)
            b_date = (due_d + timedelta(days=1)).isoformat()
            today_d = now_utc.astimezone(_tz()).date()
            c_date = _next_monday(today_d).isoformat()
            lines.append(language().messages["remind.option_b"].format(b_date=b_date))
            lines.append(language().messages["remind.option_c"].format(c_date=c_date))
        lines.append(language().messages["remind.option_d"])
        text = "\n".join(lines)
        new_count = count + 1
        if not dry_run:
            if human_channel_installed():
                human_channel().follow_up(ref, text)
            assert notes_s is not None
            updated = update_meta_field(
                notes_s, "_reminded_count", str(new_count), create=True
            )
            task_store().update_task(node.task_gid, body=updated)
            task_store().add_comment(
                node.task_gid,
                f"{text}\n\n{_REMINDED_MARKER}\n_reminded_count: {new_count}\n"
                f"ACTOR: tasksmith-tick\n"
                f"EXECUTION_ID: {_execution_id('remind', node.task_gid)}",
            )
        acted.append(node.task_gid)
    return acted


def _clear_review_request(task_gid: str) -> None:
    """Clear the `_review_requested` line and the Slack pending of a node that became READY."""
    from tasksmith import pending as loops_pending

    task = task_store().get_task(task_gid)
    body = task.get("body")
    if isinstance(body, str) and is_controlled_notes(body):
        new_body = remove_meta_field(body, "_review_requested")
        if new_body != body:
            task_store().update_task(task_gid, body=new_body)
    ref = loops_pending.find_by_loop_id(f"tasksmith:{task_gid}")
    if ref is not None:
        loops_pending.clear_question(ref)
        loops_pending.unregister(ref)
