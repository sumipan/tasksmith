"""Language pack installation (Issue #7526)."""

from __future__ import annotations

from dataclasses import replace

from tasksmith.language import language, set_language
from tasksmith.language_en import EN


def test_language_defaults_to_en_and_resets_after_custom_pack() -> None:
    """AC5: default EN; custom pack replaces it; None restores EN."""
    set_language(None)
    assert language() is EN

    other = replace(EN, meta_tip="custom tip")
    set_language(other)
    assert language() is other

    set_language(None)
    assert language() is EN


def test_en_pack_has_no_key_placeholder_strings() -> None:
    """Built-in EN strings are translated; no ``(<key>)`` message placeholders remain."""

    def bad(mapping: dict[str, str]) -> list[str]:
        return [key for key, value in mapping.items() if value == f"({key})"]

    assert not bad(dict(EN.messages))
    assert not bad(dict(EN.violations))
    assert not bad(dict(EN.prompts))
