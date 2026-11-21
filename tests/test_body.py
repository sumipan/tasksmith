"""Body format: controlled notes and metadata updates (Issue #7526)."""

from __future__ import annotations

from tasksmith.body import (
    build_controlled_notes,
    parse_meta_field,
    parse_sections,
    update_meta_field,
)
from tasksmith.language import SECTION_IDS
from tasksmith.model import OwnerType, PipelineState


def test_controlled_notes_expose_section_ids_and_meta() -> None:
    """AC1: build_controlled_notes yields all SECTION_IDS, meta fields, and expected headings."""
    body = build_controlled_notes(
        objective="Ship it",
        owner=OwnerType.HUMAN,
        state=PipelineState.READY,
        flow="dag",
        acceptance=["a", "b"],
    )
    sections = parse_sections(body)
    assert sections is not None
    assert tuple(sections.keys()) == SECTION_IDS
    assert parse_meta_field(sections, "state") == "READY"
    assert parse_meta_field(sections, "owner") == "HUMAN"
    assert parse_meta_field(sections, "flow") == "dag"
    assert parse_meta_field(sections, "format") == "tasksmith/v1"
    assert "## Objective" in body
    assert "## Acceptance criteria" in body


def test_update_meta_field_preserves_other_sections_and_meta() -> None:
    """AC2: update_meta_field changes only state; other sections and meta stay the same."""
    body = build_controlled_notes(
        objective="Ship it",
        owner=OwnerType.HUMAN,
        state=PipelineState.READY,
        flow="dag",
        acceptance=["a", "b"],
    )
    before = parse_sections(body)
    assert before is not None
    other_sections = {k: before[k] for k in before if k != "control"}
    owner_before = parse_meta_field(before, "owner")
    flow_before = parse_meta_field(before, "flow")
    format_before = parse_meta_field(before, "format")

    updated = update_meta_field(body, "state", "RUNNING")
    after = parse_sections(updated)
    assert after is not None
    assert parse_meta_field(after, "state") == "RUNNING"
    for sid, text in other_sections.items():
        assert after[sid] == text
    assert parse_meta_field(after, "owner") == owner_before
    assert parse_meta_field(after, "flow") == flow_before
    assert parse_meta_field(after, "format") == format_before
