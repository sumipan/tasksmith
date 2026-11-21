"""External-owner probes and tick integration. Refs: issue #7536 AC1–AC9."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tasksmith.body import parse_meta_field, parse_sections, update_meta_field
from tasksmith.external import (
    ProbeResult,
    format_check_at,
    new_external_body,
)
from tasksmith.model import OwnerType, PipelineState
from tasksmith.reconcile import reconcile
from tasksmith.runtime import set_human_channel, set_task_store
from tasksmith.settings import Settings, set_settings
from tasksmith.tick import tick
from tasksmith.transition import transition_task

WORKTREE = Path(__file__).resolve().parents[1]
UTC = timezone.utc


@dataclass
class RecordingChannel:
    follow_ups: list[tuple[str, str]] = field(default_factory=list)
    announces: list[str] = field(default_factory=list)

    def ask(self, text: str) -> str:
        del text
        return "1"

    def follow_up(self, ref: str, text: str) -> None:
        self.follow_ups.append((ref, text))

    def announce(self, text: str) -> None:
        self.announces.append(text)

    def link(self, ref: str) -> str | None:
        del ref
        return None


@pytest.fixture
def external_env(tmp_path, monkeypatch):
    from tasksmith import LocalTaskStore

    store_root = tmp_path / "store"
    channel = RecordingChannel()
    store = LocalTaskStore(store_root)
    set_task_store(store)
    set_human_channel(channel)
    set_settings(
        Settings(
            state_dir=tmp_path / "state",
            project_id=None,
            executor_exists=lambda _name: True,
            request_channel_id="local",
            timezone="UTC",
        )
    )
    monkeypatch.delenv("TASKSMITH_ROOT", raising=False)
    yield store, channel, tmp_path
    set_task_store(None)
    set_human_channel(None)
    set_settings(None)


def _past_external(
    *,
    check: str = "file_exists",
    check_every: int = 30,
    check_max: int = 5,
    check_at: datetime | None = None,
    params: dict[str, str] | None = None,
    report_to: str | None = None,
    window: str | None = None,
) -> str:
    at = check_at or datetime(2026, 11, 21, 10, 0, tzinfo=UTC)
    return new_external_body(
        objective="wait",
        check=check,
        check_at=at,
        check_every=check_every,
        check_max=check_max,
        check_window=window,
        params=params,
        report_to=report_to,
    )


def test_file_exists_probe_continues_then_done(external_env, tmp_path: Path) -> None:
    """Refs: #7536 AC1."""
    store, channel, _tmp = external_env
    target = tmp_path / "marker.txt"
    body = _past_external(
        check_every=30,
        report_to="r1",
        params={"path": str(target)},
    )
    gid = store.create_task("wait for file", body=body)
    t0 = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)

    tick(now=t0)
    task = store.get_task(gid)
    sections = parse_sections(task["body"])
    assert sections is not None
    assert parse_meta_field(sections, "state") == PipelineState.READY.value
    assert parse_meta_field(sections, "_checks") == "1"
    assert parse_meta_field(sections, "check_at") == format_check_at(
        t0 + timedelta(minutes=30)
    )
    assert channel.follow_ups == []

    target.write_text("ok", encoding="utf-8")
    tick(now=t0 + timedelta(minutes=30))
    sections = parse_sections(store.get_task(gid)["body"])
    assert sections is not None
    assert parse_meta_field(sections, "state") == PipelineState.DONE.value
    assert len(channel.follow_ups) == 1
    assert channel.follow_ups[0][0] == "r1"


def test_check_max_blocks_and_announces_once(external_env) -> None:
    """Refs: #7536 AC2."""
    store, channel, _tmp = external_env
    missing = "/no/such/file-7536"
    body = _past_external(check_max=2, check_every=1, params={"path": missing})
    gid = store.create_task("exhaust waits", body=body)
    now = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)

    tick(now=now)
    tick(now=now + timedelta(minutes=2))

    sections = parse_sections(store.get_task(gid)["body"])
    assert sections is not None
    assert parse_meta_field(sections, "state") == PipelineState.BLOCKED.value
    assert len(channel.announces) == 1


def test_outside_window_defers_to_next_window_start(external_env) -> None:
    """Refs: #7536 AC3."""
    store, _channel, _tmp = external_env
    body = _past_external(window="09:00-18:00")
    gid = store.create_task("windowed", body=body)
    now = datetime(2026, 11, 21, 20, 0, tzinfo=UTC)

    tick(now=now)

    sections = parse_sections(store.get_task(gid)["body"])
    assert sections is not None
    assert parse_meta_field(sections, "check_at") == "2026-11-22T09:00"
    assert parse_meta_field(sections, "_checks") == "0"


def test_settings_probe_speak_keeps_ready(external_env) -> None:
    """Refs: #7536 AC4."""
    store, channel, tmp = external_env

    def ping(_task: dict, _params: dict[str, str]) -> ProbeResult:
        return ProbeResult(status="speak", report="still waiting")

    set_settings(
        Settings(
            state_dir=tmp / "state",
            project_id=None,
            executor_exists=lambda _name: True,
            request_channel_id="local",
            timezone="UTC",
            probes={"ping": ping},
        )
    )
    body = _past_external(check="ping")
    gid = store.create_task("ping wait", body=body)
    tick(now=datetime(2026, 11, 21, 12, 0, tzinfo=UTC))

    sections = parse_sections(store.get_task(gid)["body"])
    assert sections is not None
    assert parse_meta_field(sections, "state") == PipelineState.READY.value
    assert parse_meta_field(sections, "_checks") == "1"
    assert channel.announces == ["still waiting"]


def test_dependent_ai_node_ready_after_external_done(external_env) -> None:
    """Refs: #7536 AC5."""
    from tasksmith.body import build_controlled_notes

    store, channel, _tmp = external_env
    set_human_channel(channel)
    t_check = datetime(2026, 11, 21, 11, 0, tzinfo=UTC)
    body_a = _past_external(check="time", check_at=t_check, params={"text": "done"})
    gid_a = store.create_task("external gate", body=body_a)
    body_b = build_controlled_notes(
        objective="AI after external",
        owner=OwnerType.AI,
        state=PipelineState.READY,
        flow="dag",
        executor="skill:demo",
        deliverables="- out.txt",
        acceptance=["Result in out.txt"],
    )
    body_b = update_meta_field(body_b, "depends_on", gid_a, create=True)
    gid_b = store.create_task("ai child", body=body_b)

    before = reconcile()
    assert all(n.task_gid != gid_b for n in before.ready)
    assert any(n.task_gid == gid_a for n in before.waiting_external)

    tick(now=t_check)
    after = reconcile()
    assert any(n.task_gid == gid_b for n in after.ready)


def test_unknown_check_goes_to_review(external_env) -> None:
    """Refs: #7536 AC6."""
    store, _channel, _tmp = external_env
    body = _past_external(check="nope")
    body = update_meta_field(body, "state", PipelineState.REVIEW.value)
    gid = store.create_task("bad check", body=body)

    actual = transition_task(
        gid,
        state=PipelineState.READY,
        actor="test",
        execution_id="test-7536-ac6",
    )
    assert actual == PipelineState.REVIEW
    sections = parse_sections(store.get_task(gid)["body"])
    assert parse_meta_field(sections, "state") == PipelineState.REVIEW.value
    stories = store.get_task_stories(gid)
    joined = "\n".join(str(s.get("text") or "") for s in stories)
    assert "design_doc.external_check_unknown" in joined


def test_cli_external_add_then_tick_done(external_env, tmp_path: Path) -> None:
    """Refs: #7536 AC7."""
    root = tmp_path / "cli-root"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(WORKTREE / "src")
    add = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasksmith",
            "--root",
            str(root),
            "external",
            "add",
            "--title",
            "t",
            "--check",
            "time",
            "--in",
            "0",
            "--param",
            "text=hello",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=WORKTREE,
    )
    assert add.returncode == 0, add.stderr
    gid = add.stdout.strip()

    run_tick = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasksmith",
            "--root",
            str(root),
            "--route",
            "codex",
            "tick",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=WORKTREE,
    )
    assert run_tick.returncode == 0, run_tick.stderr

    store = __import__("tasksmith", fromlist=["LocalTaskStore"]).LocalTaskStore(root)
    sections = parse_sections(store.get_task(gid)["body"])
    assert parse_meta_field(sections, "state") == PipelineState.DONE.value
    announcements = (root / ".tasks" / "announcements.md").read_text(encoding="utf-8")
    assert "hello" in announcements


def test_probe_exception_counts_and_comments(external_env) -> None:
    """Refs: #7536 AC8."""
    store, _channel, tmp = external_env

    def boom(_task: dict, _params: dict[str, str]) -> ProbeResult:
        raise RuntimeError("boom")

    set_settings(
        Settings(
            state_dir=tmp / "state",
            project_id=None,
            executor_exists=lambda _name: True,
            request_channel_id="local",
            timezone="UTC",
            probes={"boom": boom},
        )
    )
    body = _past_external(check="boom")
    gid = store.create_task("probe fails", body=body)

    tick(now=datetime(2026, 11, 21, 12, 0, tzinfo=UTC))

    sections = parse_sections(store.get_task(gid)["body"])
    assert parse_meta_field(sections, "state") == PipelineState.READY.value
    assert parse_meta_field(sections, "_checks") == "1"
    stories = store.get_task_stories(gid)
    assert any("boom" in str(s.get("text") or "") for s in stories)


def test_quiet_hours_skips_external(external_env) -> None:
    """Refs: #7536 AC9."""
    store, _channel, tmp = external_env
    body = _past_external()
    gid = store.create_task("quiet skip", body=body)
    before = store.get_task(gid)["body"]

    set_settings(
        Settings(
            state_dir=tmp / "state",
            project_id=None,
            executor_exists=lambda _name: True,
            request_channel_id="local",
            timezone="UTC",
            quiet_hours=("22:00", "07:00"),
        )
    )
    tick(now=datetime(2026, 11, 21, 23, 0, tzinfo=UTC))

    assert store.get_task(gid)["body"] == before
