"""Intake registration. Refs: issue #7778, #7906."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from tasksmith import LocalTaskStore
from tasksmith.body import parse_meta_field, parse_sections
from tasksmith.intake import mark_seen, register_items, seen_ids, unseen
from tasksmith.settings import Settings, set_settings
from tasksmith.store import TaskStoreError
from tasksmith.tags import parse_tags


def _items():
    return [
        {"text": "a"},
        {"text": "b", "owner": "AI"},
        {"text": "c", "register": False, "reason": "later"},
    ]


def _install_state(tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    set_settings(
        Settings(
            state_dir=state_dir,
            project_id=None,
            executor_exists=lambda _name: True,
        )
    )
    return state_dir / "intake-seen.jsonl"


def _read_ledger(path: Path) -> list[dict]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


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


def test_register_items_records_created_and_not_registered_outcomes(tmp_path) -> None:
    """Refs: #7906 AC1."""
    ledger = _install_state(tmp_path)
    store = LocalTaskStore(tmp_path)
    items = [
        {"text": "task one", "source_id": "made"},
        {"text": "nope", "source_id": "ignored", "register": False, "reason": "skip"},
    ]
    result = register_items(items, project_id="p", tags=["t"], store=store)
    assert len(result.created) == 1
    assert len(store.list_tasks("p")) == 1
    rows = _read_ledger(ledger)
    assert len(rows) == 2
    by_id = {row["source_id"]: row for row in rows}
    assert by_id["made"]["outcome"] == "created"
    assert by_id["made"]["task_gid"] == result.created[0]
    assert by_id["ignored"]["outcome"] == "not_registered"
    assert by_id["ignored"]["task_gid"] is None


def test_register_items_skips_seen_source_after_text_changes(tmp_path) -> None:
    """Refs: #7906 AC2."""
    ledger = _install_state(tmp_path)
    store = LocalTaskStore(tmp_path)
    tag = "weekly"
    first = [
        {"text": "alpha", "source_id": "made"},
        {"text": "hold", "source_id": "ignored", "register": False},
    ]
    register_items(first, project_id="p", tags=[tag], store=store)
    assert len(_read_ledger(ledger)) == 2
    assert len(store.list_tasks("p")) == 1

    second = [
        {"text": "beta", "source_id": "made"},
        {"text": "other", "source_id": "ignored", "register": False},
    ]
    again = register_items(second, project_id="p", tags=[tag], store=store)
    assert again.created == []
    assert len(store.list_tasks("p")) == 1
    assert len(_read_ledger(ledger)) == 2
    reasons = {row["reason"] for row in again.skipped}
    assert reasons == {"already processed"}


def test_register_items_retries_after_store_error_without_ledger_row(tmp_path) -> None:
    """Refs: #7906 AC3."""
    ledger = _install_state(tmp_path)
    store = LocalTaskStore(tmp_path)
    calls = {"n": 0}

    def flaky_create(name: str, **kwargs: object) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise TaskStoreError("transient")
        return LocalTaskStore.create_task(store, name, **kwargs)

    store.create_task = flaky_create  # type: ignore[method-assign]

    item = [{"text": "retry me", "source_id": "retry"}]
    first = register_items(item, project_id="p", tags=["t"], store=store)
    assert first.failed
    assert not ledger.exists()

    second = register_items(item, project_id="p", tags=["t"], store=store)
    assert len(second.created) == 1
    rows = _read_ledger(ledger)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "created"
    assert rows[0]["source_id"] == "retry"


def test_mark_seen_and_unseen_are_idempotent_and_ordered(tmp_path) -> None:
    """Refs: #7906 AC4."""
    ledger = _install_state(tmp_path)
    mark_seen(["b", "b"], path=ledger)
    mark_seen(["b", "b"], path=ledger)
    assert unseen(["a", "b", "a", "c"], path=ledger) == ["a", "c"]
    rows = _read_ledger(ledger)
    assert [row["source_id"] for row in rows] == ["b"]
    assert len(rows) == 1


def test_seen_ids_rejects_invalid_record_schema(tmp_path) -> None:
    ledger = _install_state(tmp_path)
    ledger.parent.mkdir(parents=True)
    invalid_rows = [
        {"source_id": "a", "outcome": "created", "task_gid": None, "at": "2026-01-01T00:00:00+00:00"},
        {"source_id": "a", "outcome": "duplicate", "task_gid": "1", "at": "2026-01-01T00:00:00+00:00"},
        {"source_id": "a", "outcome": "not_registered", "task_gid": None, "at": "not-a-time"},
        {"source_id": "a", "outcome": "not_registered", "task_gid": None, "at": "2026-01-01T00:00:00"},
    ]
    for row in invalid_rows:
        ledger.write_text(json.dumps(row) + "\n", encoding="utf-8")
        with pytest.raises(ValueError, match=r"line 1"):
            seen_ids(ledger)


def test_mark_seen_rejects_records_it_cannot_write_validly(tmp_path) -> None:
    ledger = _install_state(tmp_path)
    with pytest.raises(ValueError, match="created outcome"):
        mark_seen(["a"], outcome="created", path=ledger)
    with pytest.raises(ValueError, match="non-empty string"):
        mark_seen([""], path=ledger)
    assert not ledger.exists()


def test_register_items_without_source_id_does_not_create_ledger(tmp_path) -> None:
    """Refs: #7906 AC5."""
    ledger = _install_state(tmp_path)
    store = LocalTaskStore(tmp_path)
    register_items([{"text": "plain"}], project_id="p", tags=["t"], store=store)
    assert not ledger.exists()

    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        json.dumps(
            {
                "source_id": "x",
                "outcome": "not_registered",
                "task_gid": None,
                "at": "2026-01-01T00:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    before = ledger.read_text(encoding="utf-8")
    register_items(
        [{"text": "forced", "source_id": "y"}],
        project_id="p",
        tags=["t"],
        store=store,
        use_seen=False,
    )
    assert len(store.list_tasks("p")) == 2
    assert ledger.read_text(encoding="utf-8") == before


def test_register_items_serializes_concurrent_source_id(tmp_path) -> None:
    """Refs: #7906 AC8."""
    ledger = _install_state(tmp_path)
    store = LocalTaskStore(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    create_calls = {"n": 0}
    barrier = threading.Barrier(2)

    real_create = store.create_task

    def gated_create(name: str, **kwargs: object) -> str:
        create_calls["n"] += 1
        entered.set()
        release.wait()
        return real_create(name, **kwargs)

    store.create_task = gated_create  # type: ignore[method-assign]

    item = [{"text": "race", "source_id": "same"}]
    results: list = []
    errors: list[BaseException] = []

    def worker() -> None:
        barrier.wait()
        try:
            results.append(
                register_items(
                    item,
                    project_id="p",
                    tags=["t"],
                    store=store,
                    seen_path=ledger,
                )
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker), threading.Thread(target=worker)]
    for thread in threads:
        thread.start()
    entered.wait()
    release.set()
    for thread in threads:
        thread.join()

    assert not errors
    assert create_calls["n"] == 1
    assert len(_read_ledger(ledger)) == 1
    created_total = sum(len(r.created) for r in results)
    assert created_total == 1
    skipped_reasons = [row["reason"] for r in results for row in r.skipped]
    assert skipped_reasons.count("already processed") == 1


def test_register_items_rejects_non_string_source_id(tmp_path) -> None:
    """Refs: #7906."""
    _install_state(tmp_path)
    store = LocalTaskStore(tmp_path)
    result = register_items(
        [{"text": "bad", "source_id": 1}],
        project_id="p",
        tags=["t"],
        store=store,
    )
    assert result.failed == [{"text": "bad", "error": "invalid source_id"}]


def test_register_items_ignores_invalid_source_id_when_seen_is_disabled(tmp_path) -> None:
    """``use_seen=False`` preserves legacy processing regardless of source_id."""
    ledger = _install_state(tmp_path)
    store = LocalTaskStore(tmp_path)
    result = register_items(
        [{"text": "legacy", "source_id": 1}],
        project_id="p",
        tags=["t"],
        store=store,
        seen_path=ledger,
        use_seen=False,
    )
    assert len(result.created) == 1
    assert result.failed == []
    assert not ledger.exists()
