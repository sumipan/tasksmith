"""Intake registration. Refs: issue #7778."""

from __future__ import annotations

from tasksmith import LocalTaskStore
from tasksmith.body import parse_meta_field, parse_sections
from tasksmith.intake import register_items
from tasksmith.tags import parse_tags


def _items():
    return [
        {"text": "a"},
        {"text": "b", "owner": "AI"},
        {"text": "c", "register": False, "reason": "later"},
    ]


def test_register_items_creates_tagged_tasks(tmp_path) -> None:
    """Refs: #7778 AC1."""
    store = LocalTaskStore(tmp_path)
    tag = "2026-11-20 weekly"
    result = register_items(
        _items(),
        project_id="p",
        tags=[tag],
        store=store,
        reference="source: minutes.md",
    )
    assert len(result.created) == 2
    assert result.skipped == [{"text": "c", "reason": "later"}]
    tasks = {t["name"]: t for t in store.list_tasks("p")}
    assert set(tasks) == {"a", "b"}
    for task in tasks.values():
        assert parse_tags(task["body"]) == [tag]
        refs = parse_sections(task["body"])
        assert refs is not None
        assert "source: minutes.md" in refs["references"]
        assert parse_meta_field(refs, "state") == "REVIEW"
    assert parse_meta_field(parse_sections(tasks["a"]["body"]) or {}, "owner") == "HUMAN"
    assert parse_meta_field(parse_sections(tasks["a"]["body"]) or {}, "flow") == "personal"
    assert parse_meta_field(parse_sections(tasks["b"]["body"]) or {}, "owner") == "AI"
    assert parse_meta_field(parse_sections(tasks["b"]["body"]) or {}, "flow") == "dag"


def test_register_items_skips_already_registered(tmp_path) -> None:
    """Refs: #7778 AC2."""
    store = LocalTaskStore(tmp_path)
    kwargs = dict(project_id="p", tags=["2026-11-20 weekly"], store=store, reference="source: minutes.md")
    first = register_items(_items(), **kwargs)
    assert len(first.created) == 2

    second = register_items(_items(), **kwargs)
    assert second.created == []
    assert len(store.list_tasks("p")) == 2
    reasons = {(row["text"], row["reason"]) for row in second.skipped}
    assert ("a", "already registered") in reasons
    assert ("b", "already registered") in reasons
    assert any(row["text"] == "c" for row in second.skipped)

    store.update_task(
        next(t["gid"] for t in store.list_tasks("p") if t["name"] == "a"),
        completed=True,
    )
    third = register_items(_items(), **kwargs)
    assert third.created == []
    assert len(store.list_tasks("p")) == 2

    fourth = register_items(_items(), project_id="p", tags=["2026-11-27 weekly"], store=store, reference="source: minutes.md")
    assert len(fourth.created) == 2
    assert len(store.list_tasks("p")) == 4
