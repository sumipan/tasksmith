"""Design-document gates (Issue #7526)."""

from __future__ import annotations

from tasksmith.body import build_controlled_notes, parse_sections
from tasksmith.gates import check_design_doc
from tasksmith.language_en import EN
from tasksmith.model import OwnerType, PipelineState


def test_check_design_doc_reports_acceptance_count_violation() -> None:
    """AC3: empty acceptance section yields design_doc.ac_count with language-pack message."""
    body = build_controlled_notes(
        objective="Ship it",
        owner=OwnerType.HUMAN,
        state=PipelineState.READY,
        flow="dag",
        acceptance=["a"],
    )
    sections = parse_sections(body)
    assert sections is not None
    sections["acceptance"] = ""
    violations = check_design_doc(sections)
    ac = [v for v in violations if v.rule_id == "design_doc.ac_count"]
    assert len(ac) == 1
    expected = EN.violations["design_doc.ac_count.detail"].format(min=1, max=7, count=0)
    assert ac[0].message == expected


def test_check_design_doc_passes_for_concrete_acceptance_with_location() -> None:
    """AC4: concrete acceptance with a file path passes ac_count, vague_word, and format checks."""
    body = build_controlled_notes(
        objective="Ship it",
        owner=OwnerType.HUMAN,
        state=PipelineState.READY,
        flow="dag",
        acceptance=["The report is saved at docs/report.md"],
    )
    sections = parse_sections(body)
    assert sections is not None
    violations = check_design_doc(sections, body=body)
    forbidden = {"design_doc.ac_count", "design_doc.ac_vague_word", "design_doc.format_invalid"}
    assert forbidden.isdisjoint({v.rule_id for v in violations})
