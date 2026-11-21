"""Section classification and configuration (deterministic)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from tasksmith.model import Violation

__all__ = [
    "SectionDef",
    "SectionsConfig",
    "classify_section",
    "evaluate_classification",
    "sections_config_from_mapping",
]

ALLOWED_KEYS = frozenset({"id", "gid", "name", "default", "keywords", "patterns", "description"})


@dataclass(frozen=True)
class SectionDef:
    id: str
    gid: str
    name: str
    default: bool
    keywords: tuple[str, ...]
    patterns: tuple[re.Pattern[str], ...]
    description: str


@dataclass(frozen=True)
class SectionsConfig:
    version: int
    project_gid: str
    sections: tuple[SectionDef, ...]

    def by_id(self, section_id: str) -> SectionDef | None:
        for s in self.sections:
            if s.id == section_id:
                return s
        return None

    def default_section(self) -> SectionDef | None:
        for s in self.sections:
            if s.default:
                return s
        return None

    def normalize_id(self, value: str) -> str | None:
        """Normalize a section name to an id. Return the id unchanged when it already matches."""
        v = value.strip()
        for s in self.sections:
            if s.id == v:
                return s.id
            if s.name == v:
                return s.id
        return None

    def valid_ids(self) -> tuple[str, ...]:
        return tuple(s.id for s in self.sections)

    def non_default_section_names(self) -> list[str]:
        return [s.name for s in self.sections if not s.default]


def sections_config_from_mapping(raw: object) -> tuple[SectionsConfig | None, list[Violation]]:
    """Build a ``SectionsConfig`` from a parsed mapping (e.g. YAML or JSON loaded by the host)."""
    violations: list[Violation] = []

    if not isinstance(raw, dict):
        violations.append(Violation(rule_id="sections.load_error", message="Root must be a mapping"))
        return None, violations

    version = raw.get("version")
    project_gid = raw.get("project_gid", "")
    rows = raw.get("sections")
    if not isinstance(rows, list):
        violations.append(Violation(rule_id="sections.load_error", message="sections must be a list"))
        return None, violations

    seen_gids: set[str] = set()
    default_count = 0
    section_defs: list[SectionDef] = []

    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            violations.append(
                Violation(rule_id="sections.load_error", message=f"sections[{idx}] must be a mapping")
            )
            continue

        unknown = set(row.keys()) - ALLOWED_KEYS
        if unknown:
            violations.append(
                Violation(
                    rule_id="sections.unknown_key",
                    message=f"sections[{idx}] has unknown keys: {', '.join(sorted(unknown))}",
                )
            )
            continue

        sid = row.get("id", "")
        gid = str(row.get("gid", "")).strip()
        name = str(row.get("name", "")).strip()
        is_default = bool(row.get("default", False))
        kws_raw = row.get("keywords", [])
        pats_raw = row.get("patterns", [])
        description = str(row.get("description", "")).strip()

        if gid and gid in seen_gids:
            violations.append(
                Violation(
                    rule_id="sections.gid_duplicate",
                    message=f"duplicate gid {gid!r} (sections[{idx}])",
                )
            )
            continue
        if gid:
            seen_gids.add(gid)

        if is_default:
            default_count += 1

        if not isinstance(kws_raw, list):
            kws_raw = []
        if not isinstance(pats_raw, list):
            pats_raw = []

        keywords = tuple(str(k).strip() for k in kws_raw if str(k).strip())
        if not keywords and not pats_raw:
            if not is_default:
                violations.append(
                    Violation(
                        rule_id="sections.keywords_empty",
                        message=f"sections[{idx}] ({sid!r}) has empty keywords and patterns",
                    )
                )

        compiled: list[re.Pattern[str]] = []
        pattern_error = False
        for p in pats_raw:
            try:
                compiled.append(re.compile(str(p)))
            except re.error as exc:
                violations.append(
                    Violation(
                        rule_id="sections.pattern_invalid",
                        message=f"sections[{idx}] ({sid!r}) has invalid pattern {p!r}: {exc}",
                    )
                )
                pattern_error = True
        if pattern_error:
            continue

        section_defs.append(
            SectionDef(
                id=str(sid).strip(),
                gid=gid,
                name=name,
                default=is_default,
                keywords=keywords,
                patterns=tuple(compiled),
                description=description,
            )
        )

    if default_count == 0:
        violations.append(
            Violation(rule_id="sections.default_missing", message="No section has default: true")
        )
    elif default_count > 1:
        violations.append(
            Violation(
                rule_id="sections.default_multiple",
                message=f"{default_count} sections have default: true (only one allowed)",
            )
        )

    config = SectionsConfig(
        version=int(version) if isinstance(version, int) else 1,
        project_gid=str(project_gid).strip(),
        sections=tuple(section_defs),
    )
    return config, violations


def classify_section(
    name: str,
    objective: str,
    *,
    explicit: str | None = None,
    parent_section: str | None = None,
    config: SectionsConfig,
) -> str | None:
    """Return a section id deterministically, or ``None`` when no rule matches."""
    if explicit is not None:
        normalized = config.normalize_id(explicit)
        if normalized is None:
            return None
        return normalized

    if parent_section is not None:
        normalized = config.normalize_id(parent_section)
        if normalized is not None:
            return normalized

    text = f"{name} {objective}".lower()
    matched: list[str] = []

    for sec in config.sections:
        if sec.default:
            continue
        hit = False
        for kw in sec.keywords:
            if kw.lower() in text:
                hit = True
                break
        if not hit:
            for pat in sec.patterns:
                if pat.search(text):
                    hit = True
                    break
        if hit:
            matched.append(sec.id)

    if len(matched) == 1:
        return matched[0]

    return None


def evaluate_classification(
    tasks: list[dict[str, Any]],
    *,
    config: SectionsConfig,
) -> dict[str, Any]:
    """Aggregate classification results for a task list (dry-run evaluation)."""
    default_def = config.default_section()
    default_gid = default_def.gid if default_def else None

    by_section: dict[str, dict[str, int]] = {}
    total = 0
    wrong = 0

    for task in tasks:
        name = str(task.get("name") or "")
        body = str(task.get("body") or "")
        current_gid = str(task.get("current_section_gid") or "")

        if current_gid == default_gid:
            continue

        total += 1
        predicted_id = classify_section(name, body, config=config)

        correct_def = next((s for s in config.sections if s.gid == current_gid), None)
        correct_id = correct_def.id if correct_def else None

        sec_key = correct_def.name if correct_def else current_gid
        if sec_key not in by_section:
            by_section[sec_key] = {"matched": 0, "mismatched": 0, "no_match": 0}

        if predicted_id is None:
            by_section[sec_key]["no_match"] += 1
        elif predicted_id == correct_id:
            by_section[sec_key]["matched"] += 1
        else:
            by_section[sec_key]["mismatched"] += 1
            wrong += 1

    error_rate = wrong / total if total > 0 else 0.0
    return {
        "total": total,
        "wrong": wrong,
        "error_rate": round(error_rate, 4),
        "by_section": by_section,
    }
