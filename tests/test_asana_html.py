"""HTML notes conversion for tasksmith/v1 bodies."""

from __future__ import annotations

import re

import pytest

from tasksmith.asana.html import MAX_HTML_CHARS, body_to_html, html_to_body
from tasksmith.body import FORMAT_VERSION, build_controlled_notes, format_version
from tasksmith.store import TaskStoreError


def test_controlled_body_round_trips_without_p_tag() -> None:
    """Issue #8119: tasksmith/v1 round-trips without <p> tags."""
    body = build_controlled_notes(
        objective="Goal A",
        deliverables=["item one"],
        acceptance=["[ ] ok"],
        draft="line one\nline two",
        owner="AI",
        state="READY",
    )
    doc = body_to_html(body)
    assert not re.search(r"</?p[ >]", doc)
    assert html_to_body(doc) == body


def test_html_rejects_oversize_before_transport() -> None:
    """Issue #8119: oversize bodies fail before any HTTP client is used."""
    base = build_controlled_notes(objective="x")
    body = base.replace("## Objective\nx", "## Objective\n" + "x" * (MAX_HTML_CHARS + 1))
    with pytest.raises(TaskStoreError):
        body_to_html(body)


def test_html_ignores_legacy_and_unknown_formats() -> None:
    """Issue #8119: legacy nexus-cp and unknown html return None."""
    legacy = (
        "<body><h2>Control</h2><ul><li>format: nexus-cp/v3</li></ul>"
        "<h2>Objective</h2>x</body>"
    )
    assert html_to_body(legacy) is None
    assert html_to_body("<body>free form</body>") is None
    assert html_to_body(None) is None

    controlled = build_controlled_notes(objective="ok")
    assert format_version(controlled) == FORMAT_VERSION
