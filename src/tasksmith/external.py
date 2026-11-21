"""External-owner probes and body helpers (no state transitions)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from tasksmith.body import build_controlled_notes, parse_meta_field, update_meta_field
from tasksmith.model import OwnerType, PipelineState, Violation
from tasksmith.settings import settings

__all__ = [
    "BUILTIN_PROBES",
    "Probe",
    "ProbeResult",
    "external_violations",
    "format_check_at",
    "in_window",
    "new_external_body",
    "next_window_start",
    "parse_check_at",
    "parse_check_params",
    "resolve_probe",
]

_CHECK_AT_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})(?:\:(\d{2}))?(?:Z|[+-]\d{2}:\d{2})?$"
)
_WINDOW_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")


@dataclass(frozen=True)
class ProbeResult:
    status: str  # done | continue | speak
    report: str = ""


Probe = Callable[[dict[str, Any], dict[str, str]], ProbeResult]


def _tz() -> ZoneInfo:
    return ZoneInfo(settings().timezone)


def _parse_hhmm(pair: str) -> tuple[int, int]:
    h, m = pair.split(":", 1)
    return int(h), int(m)


def parse_check_at(value: str) -> datetime:
    """Parse check_at into an aware datetime (Settings.timezone when no offset)."""
    raw = (value or "").strip()
    if not raw:
        raise ValueError("empty check_at")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        if "T" in raw and ("+" in raw[10:] or "-" in raw[10:]):
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_tz())
            return dt
    except ValueError:
        pass
    m = _CHECK_AT_RE.match(raw)
    if not m:
        raise ValueError(f"invalid check_at: {value!r}")
    y, mo, d = (int(x) for x in m.group(1).split("-"))
    hh, mm = int(m.group(2)), int(m.group(3))
    return datetime(y, mo, d, hh, mm, tzinfo=_tz())


def format_check_at(dt: datetime) -> str:
    local = dt.astimezone(_tz()).replace(second=0, microsecond=0)
    return local.strftime("%Y-%m-%dT%H:%M")


def _minutes_since_midnight(h: int, m: int) -> int:
    return h * 60 + m


def in_window(now: datetime, window: str) -> bool:
    """Whether ``now`` falls inside ``HH:MM-HH:MM`` in Settings.timezone."""
    m = _WINDOW_RE.match(window.strip())
    if not m:
        raise ValueError(f"invalid check_window: {window!r}")
    sh, sm = int(m.group(1)), int(m.group(2))
    eh, em = int(m.group(3)), int(m.group(4))
    local = now.astimezone(_tz())
    now_min = _minutes_since_midnight(local.hour, local.minute)
    start = _minutes_since_midnight(sh, sm)
    end = _minutes_since_midnight(eh, em)
    if start <= end:
        return start <= now_min < end
    return now_min >= start or now_min < end


def next_window_start(now: datetime, window: str) -> datetime:
    """Next window opening strictly after ``now`` (minute resolution)."""
    m = _WINDOW_RE.match(window.strip())
    if not m:
        raise ValueError(f"invalid check_window: {window!r}")
    sh, sm = int(m.group(1)), int(m.group(2))
    local = now.astimezone(_tz()).replace(second=0, microsecond=0)
    candidate = local.replace(hour=sh, minute=sm)
    if candidate <= local or in_window(local, window):
        candidate = candidate + timedelta(days=1)
    return candidate


def _time_probe(_task: dict[str, Any], params: dict[str, str]) -> ProbeResult:
    return ProbeResult(status="done", report=params.get("text", ""))


def _file_exists_probe(_task: dict[str, Any], params: dict[str, str]) -> ProbeResult:
    path = Path(params["path"]).expanduser()
    if path.exists():
        return ProbeResult(status="done")
    return ProbeResult(status="continue")


BUILTIN_PROBES: Mapping[str, Probe] = {
    "time": _time_probe,
    "file_exists": _file_exists_probe,
}


def resolve_probe(name: str) -> Probe | None:
    key = (name or "").strip()
    if not key:
        return None
    custom = settings().probes.get(key)
    if custom is not None:
        return custom
    return BUILTIN_PROBES.get(key)


def _parse_check_params(raw: str | None) -> dict[str, str] | None:
    if raw is None or not str(raw).strip():
        return {}
    try:
        data = json.loads(str(raw).strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    out: dict[str, str] = {}
    for k, v in data.items():
        if not isinstance(k, str) or not isinstance(v, str):
            return None
        out[k] = v
    return out


def _positive_int(raw: str | None) -> int | None:
    if raw is None or not str(raw).strip():
        return None
    try:
        val = int(str(raw).strip())
    except ValueError:
        return None
    return val if val >= 1 else None


def external_violations(sections: dict[str, str]) -> list[Violation]:
    from tasksmith.language import language

    texts = language().violations
    violations: list[Violation] = []
    check = parse_meta_field(sections, "check")
    if not check or resolve_probe(check) is None:
        violations.append(
            Violation(
                rule_id="design_doc.external_check_unknown",
                message=texts["design_doc.external_check_unknown.detail"],
            )
        )
    check_at_raw = parse_meta_field(sections, "check_at")
    try:
        parse_check_at(check_at_raw or "")
    except ValueError:
        violations.append(
            Violation(
                rule_id="design_doc.external_meta_invalid",
                message=texts["design_doc.external_meta_invalid.detail"].format(field="check_at"),
            )
        )
    for field in ("check_every", "check_max"):
        if _positive_int(parse_meta_field(sections, field)) is None:
            violations.append(
                Violation(
                    rule_id="design_doc.external_meta_invalid",
                    message=texts["design_doc.external_meta_invalid.detail"].format(field=field),
                )
            )
    window = parse_meta_field(sections, "check_window")
    if window and not _WINDOW_RE.match(window.strip()):
        violations.append(
            Violation(
                rule_id="design_doc.external_meta_invalid",
                message=texts["design_doc.external_meta_invalid.detail"].format(
                    field="check_window"
                ),
            )
        )
    params_raw = parse_meta_field(sections, "check_params")
    if params_raw:
        parsed = _parse_check_params(params_raw)
        if parsed is None:
            violations.append(
                Violation(
                    rule_id="design_doc.external_meta_invalid",
                    message=texts["design_doc.external_meta_invalid.detail"].format(
                        field="check_params"
                    ),
                )
            )
    return violations


def new_external_body(
    *,
    objective: str,
    check: str,
    check_at: datetime,
    check_every: int = 60,
    check_max: int = 5,
    check_window: str | None = None,
    params: Mapping[str, str] | None = None,
    report_to: str | None = None,
) -> str:
    body = build_controlled_notes(
        objective=objective,
        owner=OwnerType.EXTERNAL,
        state=PipelineState.READY,
        flow="dag",
    )
    body = update_meta_field(body, "check", check, create=True)
    body = update_meta_field(body, "check_at", format_check_at(check_at), create=True)
    body = update_meta_field(body, "check_every", str(check_every), create=True)
    body = update_meta_field(body, "check_max", str(check_max), create=True)
    body = update_meta_field(body, "_checks", "0", create=True)
    if check_window:
        body = update_meta_field(body, "check_window", check_window, create=True)
    if params:
        body = update_meta_field(
            body, "check_params", json.dumps(dict(params), ensure_ascii=False), create=True
        )
    if report_to:
        body = update_meta_field(body, "report_to", report_to, create=True)
    return body


def parse_check_params(sections: dict[str, str]) -> dict[str, str]:
    raw = parse_meta_field(sections, "check_params")
    parsed = _parse_check_params(raw)
    return parsed if parsed is not None else {}
