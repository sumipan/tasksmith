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
