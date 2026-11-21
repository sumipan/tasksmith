"""Section configuration and classification (Issue #7526)."""

from __future__ import annotations

from tasksmith.sections import SectionsConfig, classify_section, sections_config_from_mapping


def _two_section_config() -> SectionsConfig:
    cfg, violations = sections_config_from_mapping(
        {
            "version": 1,
            "sections": [
                {"id": "inbox", "name": "Inbox", "default": True},
                {"id": "health", "name": "Health", "keywords": ["gym"]},
            ],
        }
    )
    assert cfg is not None
    assert not violations
    return cfg


def test_classify_section_matches_keywords_or_returns_none() -> None:
    """AC6: keyword match returns section id; no match returns None."""
    cfg = _two_section_config()
    assert classify_section("Go to the gym", "", config=cfg) == "health"
    assert classify_section("Write the memo", "", config=cfg) is None


def test_sections_config_from_mapping_requires_default_section() -> None:
    """AC7: valid mapping builds config; missing default yields sections.default_missing."""
    cfg, violations = sections_config_from_mapping(
        {
            "version": 1,
            "sections": [
                {"id": "inbox", "name": "Inbox", "default": True},
                {"id": "health", "name": "Health", "keywords": ["gym"]},
            ],
        }
    )
    assert cfg is not None
    assert len(cfg.sections) == 2
    assert violations == []

    cfg2, violations2 = sections_config_from_mapping(
        {
            "version": 1,
            "sections": [
                {"id": "inbox", "name": "Inbox"},
                {"id": "health", "name": "Health", "keywords": ["gym"]},
            ],
        }
    )
    assert cfg2 is not None
    assert any(v.rule_id == "sections.default_missing" for v in violations2)
