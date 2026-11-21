"""Task body format (``tasksmith/v1``): a fixed plain-text (Markdown) layout."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from tasksmith.language import SECTION_IDS, language

__all__ = [
    "EDITABLE_SECTIONS",
    "FORMAT_MARKER",
    "FORMAT_VERSION",
    "build_controlled_notes",
    "format_version",
    "is_controlled_notes",
    "new_controlled_notes",
    "parse_meta_field",
    "parse_sections",
    "parse_start_on",
    "remove_meta_field",
    "replace_editable_sections",
    "section_items",
    "serialize_sections",
    "set_start_on",
    "update_meta_field",
]

FORMAT_VERSION = "tasksmith/v1"
FORMAT_MARKER = f"format: {FORMAT_VERSION}"
EDITABLE_SECTIONS = ("draft", "acceptance")

_NOT_CONTROLLED = "Body is not in the fixed tasksmith format."
_HEADING_LINE_RE = re.compile(r"^## (.+?)\s*$")
_FENCE_RE = re.compile(r"^\s*(`{3,})")
_META_LINE_RE = re.compile(r"^-[ \t]+([^:\n]+?):[ \t]*(.*?)[ \t]*$")
_ITEM_RE = re.compile(r"^\s*[-*][ \t]+(.*?)\s*$")
_START_ON_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9_.-]+/v\d+$")


def _one_line(value: str) -> str:
    return " ".join((value or "").split())


def _fence_for(text: str) -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def parse_sections(body: str | None) -> dict[str, str] | None:
    """Split a body into sections keyed by section id."""
    if not body or not isinstance(body, str):
        return None
    id_by_heading = {text: sid for sid, text in language().headings.items()}
    lines = body.replace("\r\n", "\n").split("\n")
    names: list[str] = []
    chunks: list[list[str]] = []
    fence_len = 0
    for line in lines:
        fence_m = _FENCE_RE.match(line)
        if fence_m:
            n = len(fence_m.group(1))
            if fence_len == 0:
                fence_len = n
            elif n >= fence_len and line.strip() == "`" * n:
                fence_len = 0
        if fence_len == 0 and not fence_m:
            head = _HEADING_LINE_RE.match(line)
            if head and head.group(1) in id_by_heading:
                names.append(id_by_heading[head.group(1)])
                chunks.append([])
                continue
        if not chunks:
            if line.strip():
                return None
            continue
        chunks[-1].append(line)
    if tuple(names) != SECTION_IDS:
        return None
    return {name: "\n".join(chunk).strip("\n") for name, chunk in zip(names, chunks)}


def serialize_sections(sections: dict[str, str]) -> str:
    """Turn a section-id keyed dict into a body (sections in :data:`SECTION_IDS` order)."""
    headings = language().headings
    parts: list[str] = []
    for sid in SECTION_IDS:
        heading = headings[sid]
        text = (sections.get(sid) or "").strip("\n")
        parts.append(f"## {heading}\n{text}" if text else f"## {heading}")
    return "\n\n".join(parts) + "\n"


def parse_meta_field(sections: dict[str, str], key: str) -> str | None:
    """Read the value of a ``- key: value`` line in the control metadata."""
    for line in (sections.get("control") or "").splitlines():
        m = _META_LINE_RE.match(line.strip())
        if m and m.group(1).strip() == key:
            return m.group(2).strip() or None
    return None


def format_version(body: str | None) -> str | None:
    """Name of the body format version (e.g. ``tasksmith/v1``); ``None`` if not in the fixed format."""
    sections = parse_sections(body)
    if sections is None:
        return None
    value = parse_meta_field(sections, "format")
    if value is None or not _VERSION_RE.match(value):
        return None
    return value


def is_controlled_notes(body: str | None) -> bool:
    """True if the body is in the fixed format with version ``tasksmith/v1``."""
    return format_version(body) == FORMAT_VERSION


def section_items(section_text: str | None) -> list[str]:
    """Extract the items of lines starting with ``- `` (``[ ]`` / ``[x]`` are kept)."""
    items: list[str] = []
    for line in (section_text or "").splitlines():
        m = _ITEM_RE.match(line)
        if m and m.group(1):
            items.append(m.group(1))
    return items


def _enum_value(x: Any) -> str:
    return str(getattr(x, "value", x))


def _checklist(items: Sequence[str]) -> str:
    cleaned: list[str] = []
    for raw in items:
        line = _one_line(raw)
        if not line:
            continue
        line = re.sub(r"^[-*]\s+", "", line)
        if not re.match(r"^\[( |x|X)\]\s", line):
            line = f"[ ] {line}"
        cleaned.append(line)
    if not cleaned:
        cleaned = [f"[ ] {language().messages['body_format.checklist_empty']}"]
    return "\n".join(f"- {x}" for x in cleaned)


def _acceptance_text(acceptance: str | Sequence[str] | None) -> str:
    if acceptance is None:
        return _checklist([])
    if isinstance(acceptance, str):
        return _checklist(acceptance.splitlines())
    return _checklist(acceptance)


def _deliverables_text(deliverables: str | Sequence[str]) -> str:
    if isinstance(deliverables, str):
        return deliverables.strip()
    return "\n".join(f"- {_one_line(str(x))}" for x in deliverables if str(x).strip())


def build_controlled_notes(
    *,
    objective: str,
    draft: str = "",
    acceptance: str | Sequence[str] | None = None,
    context: str = "",
    owner: Any = "AI",
    state: Any = "REVIEW",
    executor: str | None = None,
    original_notes: str | None = None,
    deliverables: str = "",
    procedure: str = "",
    judgments: str = "",
    inputs: str = "",
    outputs: str = "",
    flow: str | None = None,
    location: str | None = None,
) -> str:
    """Build a body in the fixed ``tasksmith/v1`` format."""
    meta_lines = [
        language().meta_tip,
        f"- {FORMAT_MARKER}",
        f"- state: {_enum_value(state)}",
        f"- owner: {_enum_value(owner)}",
    ]
    if flow is not None:
        meta_lines.append(f"- flow: {_one_line(flow)}")
    if location is not None:
        meta_lines.append(f"- location: {_one_line(location)}")
    meta_lines.append(f"- executor: {_one_line(executor or '')}".rstrip())
    meta_lines.append(f"- inputs: {_one_line(inputs)}".rstrip())
    meta_lines.append(f"- outputs: {_one_line(outputs)}".rstrip())

    reference = context.strip()
    if original_notes and original_notes.strip():
        original = original_notes.rstrip()
        fence = _fence_for(original)
        block = f"### {language().original_notes_heading}\n{fence}\n{original}\n{fence}"
        reference = f"{reference}\n{block}" if reference else block

    return serialize_sections(
        {
            "control": "\n".join(meta_lines),
            "objective": objective.strip(),
            "deliverables": _deliverables_text(deliverables),
            "procedure": procedure.strip(),
            "acceptance": _acceptance_text(acceptance),
            "judgments": judgments.strip(),
            "draft": draft.strip(),
            "references": reference,
        }
    )


def new_controlled_notes(*, objective: str, context: str = "") -> str:
    """Build a new task body in which the AI can tell the protected areas apart."""
    return build_controlled_notes(objective=objective, context=context)


def _require_sections(body: str) -> dict[str, str]:
    sections = parse_sections(body)
    if sections is None or not is_controlled_notes(body):
        raise ValueError(_NOT_CONTROLLED)
    return sections


def replace_editable_sections(
    body: str,
    *,
    draft: str | None = None,
    acceptance: str | Sequence[str] | None = None,
    deliverables: str | Sequence[str] | None = None,
) -> str:
    """Replace only the editable sections of a fixed-format body."""
    sections = _require_sections(body)
    if draft is not None:
        sections["draft"] = draft.strip()
    if acceptance is not None:
        sections["acceptance"] = _acceptance_text(acceptance)
    if deliverables is not None:
        sections["deliverables"] = _deliverables_text(deliverables)
    return serialize_sections(sections)


def update_meta_field(body: str, key: str, value: str, *, create: bool = False) -> str:
    """Replace only the ``- key: ...`` line of the control metadata with a new value."""
    sections = _require_sections(body)
    meta_lines = (sections["control"]).split("\n")
    new_line = f"- {key}: {_one_line(value)}".rstrip()
    for i, line in enumerate(meta_lines):
        m = _META_LINE_RE.match(line.strip())
        if m and m.group(1).strip() == key:
            meta_lines[i] = new_line
            sections["control"] = "\n".join(meta_lines)
            return serialize_sections(sections)
    if not create:
        raise ValueError(f"Control metadata has no {key}: line.")
    meta_lines = [ln for ln in meta_lines if ln != ""] + [new_line]
    sections["control"] = "\n".join(meta_lines)
    return serialize_sections(sections)


def remove_meta_field(body: str, key: str) -> str:
    """Delete one ``- key: ...`` line of the control metadata; return the input unchanged if absent."""
    sections = _require_sections(body)
    meta_lines = sections["control"].split("\n")
    for i, line in enumerate(meta_lines):
        m = _META_LINE_RE.match(line.strip())
        if m and m.group(1).strip() == key:
            del meta_lines[i]
            sections["control"] = "\n".join(meta_lines)
            return serialize_sections(sections)
    return body


def parse_start_on(body: str | None) -> str | None:
    """Read ``start_on:`` of the control metadata as ``YYYY-MM-DD``; ``None`` if uncontrolled or malformed."""
    if not body or not is_controlled_notes(body):
        return None
    sections = parse_sections(body)
    if sections is None:
        return None
    value = parse_meta_field(sections, "start_on")
    if value is None or not _START_ON_RE.match(value):
        return None
    return value


def set_start_on(body: str, start_on: str | None) -> str:
    """Replace or insert the ``start_on:`` line; ``None`` deletes the line."""
    if start_on is None:
        return remove_meta_field(body, "start_on")
    return update_meta_field(body, "start_on", start_on, create=True)
