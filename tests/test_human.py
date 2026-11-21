"""Human reply handling. Refs: issue #7528 AC2–AC4."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date

import pytest

from tasksmith import pending
from tasksmith.body import build_controlled_notes, parse_meta_field, parse_sections, update_meta_field
from tasksmith.execution import EngineUnavailable, set_thinker
from tasksmith.human import record_human_reply
from tasksmith.model import OwnerType, PipelineState
from tasksmith.runtime import set_human_channel, set_task_store
from tasksmith.settings import Settings, set_settings


@dataclass
class ScriptedThinker:
    response: str

    def ask(self, prompt: str, *, weight: str, timeout: float) -> str:
        del prompt, weight, timeout
        return self.response


@dataclass
class RecordingChannel:
    follow_ups: list[tuple[str, str]] = field(default_factory=list)

    def ask(self, text: str) -> str:
        del text
        return "1"

    def follow_up(self, ref: str, text: str) -> None:
        self.follow_ups.append((ref, text))

    def announce(self, text: str) -> None:
        del text

    def link(self, ref: str) -> str | None:
        del ref
        return None


@pytest.fixture
def human_env(tmp_path):
    from tasksmith import LocalTaskStore

    store_root = tmp_path / "store"
    state_dir = tmp_path / "state"
    store = LocalTaskStore(store_root)
    channel = RecordingChannel()
    set_task_store(store)
    set_human_channel(channel)
    set_settings(
        Settings(
            state_dir=state_dir,
            project_id=None,
            executor_exists=lambda _name: True,
            request_channel_id="local-file",
        )
    )
    yield store, channel
    set_task_store(None)
    set_human_channel(None)
    set_settings(None)
    set_thinker(None)


def _waiting_body() -> str:
    body = build_controlled_notes(
        objective="human gate",
        owner=OwnerType.HUMAN,
        state=PipelineState.WAITING_HUMAN,
        flow="dag",
    )
    return update_meta_field(body, "_request_id", "req-1", create=True)


def test_record_human_reply_defer_keeps_waiting_and_extends_due(human_env) -> None:
    """Refs: #7528 AC2."""
    store, _ch = human_env
    gid = store.create_task("human gate", body=_waiting_body(), due_on="2026-11-20")
    pending.register("1", f"tasksmith:{gid}")
    payload = json.dumps(
        {"decision": "defer", "choice": "", "note": "", "due_on": "2026-11-27"}
    )
    set_thinker(ScriptedThinker(f"```json\n{payload}\n```"))

    result = record_human_reply(
        gid,
        "next week",
        ref="1",
        today=date(2026, 11, 21),
    )
    assert result.transitioned_to == PipelineState.WAITING_HUMAN.value
    task = store.get_task(gid)
    assert task["due_on"] == "2026-11-27"
    secs = parse_sections(task["body"])
    assert secs is not None
    assert parse_meta_field(secs, "state") == PipelineState.WAITING_HUMAN.value
    assert pending.lookup("1") is not None


def test_record_human_reply_reject_blocks_and_unregisters(human_env) -> None:
    """Refs: #7528 AC3."""
    store, _ch = human_env
    gid = store.create_task("human gate", body=_waiting_body())
    pending.register("1", f"tasksmith:{gid}")
    payload = json.dumps({"decision": "reject", "choice": "", "note": "", "due_on": ""})
    set_thinker(ScriptedThinker(f"```json\n{payload}\n```"))

    result = record_human_reply(gid, "no", ref="1", today=date(2026, 11, 21))
    assert result.transitioned_to == PipelineState.BLOCKED.value
    secs = parse_sections(store.get_task(gid)["body"])
    assert secs is not None
    assert parse_meta_field(secs, "state") == PipelineState.BLOCKED.value
    assert pending.lookup("1") is None


def test_record_human_reply_engine_unavailable_follows_up_once(human_env) -> None:
    """Refs: #7528 AC4."""

    @dataclass
    class UnavailableThinker:
        def ask(self, prompt: str, *, weight: str, timeout: float) -> str:
            del prompt, weight, timeout
            raise EngineUnavailable("paused")

    store, channel = human_env
    gid = store.create_task("human gate", body=_waiting_body())
    pending.register("1", f"tasksmith:{gid}")
    set_thinker(UnavailableThinker())
    before = store.get_task(gid)["body"]

    with pytest.raises(EngineUnavailable):
        record_human_reply(gid, "ok", ref="1")

    assert store.get_task(gid)["body"] == before
    assert len(channel.follow_ups) == 1
    assert channel.follow_ups[0][0] == "1"
