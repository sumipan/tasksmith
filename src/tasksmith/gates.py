"""Design-document gates and violation formatting."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from tasksmith.body import (
    FORMAT_VERSION,
    format_version,
    parse_meta_field,
    section_items,
)
from tasksmith.language import SECTION_IDS, language
from tasksmith.model import FLOW_DAG, OwnerType, Violation
from tasksmith.settings import settings

__all__ = [
    "AC_MAX_COUNT",
    "AC_MIN_COUNT",
    "check_design_doc",
    "format_violations_comment",
]

AC_MIN_COUNT = 1
AC_MAX_COUNT = 7

_URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>\"]+")
_FILE_PATH_RE = re.compile(
    r"(?:[A-Za-z]:[\\/]|/(?:[\w.-]+/)+[\w.-]+|(?:^|\s)(?:[\w.-]+/)+[\w.-]+|"
    r"[\w.-]+\.(?:md|txt|pdf|docx?|xlsx?|csv|json|ya?ml)\b)",
    re.I,
)
_SKILL_EXECUTOR_RE = re.compile(r"^skill:([A-Za-z0-9][A-Za-z0-9_-]*)$")


def _deliverable_items(text: str) -> list[str]:
    items = section_items(text)
    if items:
        return items
    plain = (text or "").strip()
    return [line.strip() for line in plain.splitlines() if line.strip()]


def _has_location(item: str) -> bool:
    if _URL_IN_TEXT_RE.search(item):
        return True
    if _FILE_PATH_RE.search(item):
        return True
    return any(kw in item for kw in language().word_lists["location_keywords"])


def format_violations_comment(violations: Sequence[Violation]) -> str:
    """Render the list of rule_id and message as comment text."""
    if not violations:
        return ""
    return "\n".join(f"- [{v.rule_id}] {v.message}" for v in violations)


def _sections_format(sections: dict[str, str]) -> str | None:
    meta = sections.get("control") or ""
    m = re.search(r"^-\s+format:\s*(\S+)\s*$", meta, re.M)
    return m.group(1) if m else None


def check_design_doc(
    sections: dict[str, str],
    *,
    body: str | None = None,
    sections_config: Any = None,
    is_subtask: bool = False,
) -> list[Violation]:
    """Deterministic gate for a ``tasksmith/v1`` body."""
    texts = language().violations
    violations: list[Violation] = []
    keys = tuple(sections.keys())
    ver = format_version(body) if body is not None else _sections_format(sections)

    if ver == FORMAT_VERSION and keys == SECTION_IDS:
        pass
    elif ver is not None and ver != FORMAT_VERSION and keys == SECTION_IDS:
        violations.append(
            Violation(
                rule_id="design_doc.format_outdated",
                message=texts["design_doc.format_outdated.detail"].format(
                    version=ver, current=FORMAT_VERSION
                ),
                auto_fixable=True,
            )
        )
    else:
        violations.append(
            Violation(
                rule_id="design_doc.format_invalid",
                message=texts["design_doc.format_invalid.detail"].format(current=FORMAT_VERSION),
            )
        )

    judgment = (sections.get("judgments") or "").strip()
    if judgment:
        violations.append(
            Violation(
                rule_id="design_doc.judgment_not_empty",
                message=texts["design_doc.judgment_not_empty.detail"],
            )
        )

    flow = parse_meta_field(sections, "flow")
    owner = parse_meta_field(sections, "owner")
    is_dag = flow == FLOW_DAG
    is_personal = not is_dag

    if is_personal:
        objective = (sections.get("objective") or "").strip()
        if not objective:
            violations.append(
                Violation(
                    rule_id="design_doc.objective_empty",
                    message=texts["design_doc.objective_empty.detail"],
                )
            )
        _check_section_meta(sections, sections_config=sections_config, is_subtask=is_subtask, violations=violations)
        return violations

    ac_items = section_items(sections.get("acceptance") or "")
    if not (AC_MIN_COUNT <= len(ac_items) <= AC_MAX_COUNT):
        violations.append(
            Violation(
                rule_id="design_doc.ac_count",
                message=texts["design_doc.ac_count.detail"].format(
                    min=AC_MIN_COUNT, max=AC_MAX_COUNT, count=len(ac_items)
                ),
            )
        )
    for item in ac_items:
        for word in language().word_lists["ac_vague"]:
            if word in item:
                violations.append(
                    Violation(
                        rule_id="design_doc.ac_vague_word",
                        message=texts["design_doc.ac_vague_word.detail"].format(word=word, item=item),
                    )
                )
                break

    if owner == OwnerType.AI.value:
        executor = parse_meta_field(sections, "executor")
        executor = (executor or "").strip()
        skill_m = _SKILL_EXECUTOR_RE.fullmatch(executor)
        if not skill_m:
            violations.append(
                Violation(
                    rule_id="design_doc.executor_not_found",
                    message=texts["design_doc.executor_not_found.bad_format"],
                )
            )
        else:
            skill_name = skill_m.group(1).strip()
            if not skill_name or not settings().executor_exists(skill_name):
                violations.append(
                    Violation(
                        rule_id="design_doc.executor_not_found",
                        message=texts["design_doc.executor_not_found.missing_skill"].format(
                            skill_name=skill_name
                        ),
                    )
                )

        deliverable_items = _deliverable_items(sections.get("deliverables") or "")
        if not deliverable_items:
            violations.append(
                Violation(
                    rule_id="design_doc.deliverable_no_location",
                    message=texts["design_doc.deliverable_no_location.empty"],
                )
            )
        for item in deliverable_items:
            if not _has_location(item):
                violations.append(
                    Violation(
                        rule_id="design_doc.deliverable_no_location",
                        message=texts["design_doc.deliverable_no_location.item"].format(item=item),
                    )
                )

    _check_section_meta(sections, sections_config=sections_config, is_subtask=is_subtask, violations=violations)
    return violations


def _check_section_meta(
    sections: dict[str, str],
    *,
    sections_config: Any,
    is_subtask: bool,
    violations: list[Violation],
) -> None:
    """Deterministic check of the section: metadata (no-op when sections_config is None)."""
    if sections_config is None:
        return
    texts = language().violations
    section_val = parse_meta_field(sections, "section")
    if section_val is None:
        if not is_subtask:
            violations.append(
                Violation(
                    rule_id="design_doc.section_missing",
                    message=texts["design_doc.section_missing.detail"],
                )
            )
        return
    normalized = sections_config.normalize_id(section_val)
    if normalized is None:
        violations.append(
            Violation(
                rule_id="design_doc.section_unknown",
                message=texts["design_doc.section_unknown.detail"].format(value=section_val),
            )
        )
        return
    sec_def = sections_config.by_id(normalized)
    if sec_def is not None and sec_def.default:
        violations.append(
            Violation(
                rule_id="design_doc.section_default",
                message=texts["design_doc.section_default.detail"].format(name=sec_def.name),
            )
        )
