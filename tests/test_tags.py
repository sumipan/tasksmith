"""Tag metadata and summarize. Refs: issue #7778."""

from __future__ import annotations

from datetime import datetime

import pytest

from tasksmith import LocalTaskStore
from tasksmith.body import build_controlled_notes, parse_meta_field, parse_sections
from tasksmith.model import PipelineState
from tasksmith.tags import parse_tags, set_tags, summarize


def test_set_tags_round_trip() -> None:
    """Refs: #7778 AC3."""
    body = build_controlled_notes(objective="o", state=PipelineState.REVIEW)
    control_before = parse_sections(body)["control"]
    tagged = set_tags(body, ["x", "y"])
    assert parse_tags(tagged) == ["x", "y"]
    sections = parse_sections(tagged)
    assert sections is not None
    assert sections["control"].startswith(control_before.split("\n")[0])
    assert parse_meta_field(sections, "tags") == "x, y"
    for sid in ("objective", "deliverables", "procedure", "acceptance", "judgments", "draft", "references"):
        assert sections[sid] == parse_sections(body)[sid]

    cleared = set_tags(tagged, [])
    assert parse_meta_field(parse_sections(cleared) or {}, "tags") is None

    with pytest.raises(ValueError):
        set_tags(body, ["a,b"])


def test_summarize_finishes_when_all_done(tmp_path) -> None:
    """Refs: #7778 AC4."""
    store = LocalTaskStore(tmp_path)
    tag = "t"
    body = set_tags(build_controlled_notes(objective="1", state=PipelineState.REVIEW), [tag])
    one = store.create_task("one", project_gid="p", body=body)
    two = store.create_task("two", project_gid="p", body=body)

    groups = summarize(store.list_tasks("p"))
    by_tag = {g.tag: g for g in groups}
    assert by_tag[tag].finished is False
    assert len(by_tag[tag].open) == 2
    assert len(by_tag[tag].done) == 0
    assert by_tag[tag].finished_at is None

    store.update_task(one, completed=True)
    groups = summarize(store.list_tasks("p"))
    by_tag = {g.tag: g for g in groups}
    assert by_tag[tag].finished is False
    assert len(by_tag[tag].open) == 1
    assert len(by_tag[tag].done) == 1

    store.update_task(two, completed=True)
    finished_at = store.get_task(two)["completed_at"]
    groups = summarize(store.list_tasks("p"))
    by_tag = {g.tag: g for g in groups}
    assert by_tag[tag].finished is True
    assert by_tag[tag].finished_at == finished_at
    assert datetime.fromisoformat(by_tag[tag].finished_at.replace("Z", "+00:00"))
