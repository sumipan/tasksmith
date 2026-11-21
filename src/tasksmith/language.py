"""Language pack: user-facing phrases and vocabulary for the tasksmith core."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

__all__ = [
    "LanguagePack",
    "SECTION_IDS",
    "language",
    "language_installed",
    "set_language",
]

# Order of the sections in a body (the last one is the control metadata).
SECTION_IDS: tuple[str, ...] = (
    "objective",
    "deliverables",
    "procedure",
    "acceptance",
    "judgments",
    "draft",
    "references",
    "control",
)


@dataclass(frozen=True)
class LanguagePack:
    headings: Mapping[str, str]
    original_notes_heading: str
    meta_tip: str
    messages: Mapping[str, str]
    violations: Mapping[str, str]
    word_lists: Mapping[str, tuple[str, ...]]
    prompts: Mapping[str, str]


_PACK: LanguagePack | None = None


def language() -> LanguagePack:
    """Return the installed pack, or the built-in English pack when none is installed."""
    if _PACK is None:
        from tasksmith.language_en import EN

        return EN
    return _PACK


def set_language(pack: LanguagePack | None) -> None:
    """Install ``pack`` (``None`` restores the built-in English pack)."""
    global _PACK
    _PACK = pack


def language_installed() -> bool:
    return _PACK is not None
