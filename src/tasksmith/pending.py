"""tasksmith pending map: ref -> loop_id / question_id / delivered_event_ids.

Moved from the old `tools.loops_host.pending` and dedicated to tasksmith (Human-as-a-Node).
Persisted to `jobs/tasksmith/pending.json` (#3320).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tasksmith.settings import settings


def pending_path() -> Path:
    return settings().state_dir / "pending.json"


@dataclass(frozen=True)
class PendingLoop:
    loop_id: str
    question_id: str | None = None


def _load_map() -> dict[str, Any]:
    path = pending_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[tasksmith_pending] load failed ({path}): {e}", file=sys.stderr)
        return {}
    if not isinstance(raw, dict):
        print(f"[tasksmith_pending] invalid type (expected dict): {type(raw)!r}", file=sys.stderr)
        return {}
    return raw


def _atomic_write(data: dict[str, Any]) -> None:
    path = pending_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".pending-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _entry_from_raw(raw: Any) -> PendingLoop | None:
    if not isinstance(raw, dict):
        return None
    loop_id = raw.get("loop_id")
    if not isinstance(loop_id, str) or not loop_id:
        return None
    qid = raw.get("question_id")
    if qid is not None and not isinstance(qid, str):
        qid = None
    if isinstance(qid, str) and not qid:
        qid = None
    return PendingLoop(loop_id=loop_id, question_id=qid)


def _delivered_ids(entry: dict[str, Any]) -> list[str]:
    raw = entry.get("delivered_event_ids")
    if not isinstance(raw, list):
        return []
    return [x for x in raw if isinstance(x, str) and x]


def register(ref: str, loop_id: str) -> None:
    data = _load_map()
    existing = data.get(ref)
    delivered_event_ids = _delivered_ids(existing) if isinstance(existing, dict) else []
    data[ref] = {
        "loop_id": loop_id,
        "question_id": None,
        "delivered_event_ids": delivered_event_ids,
    }
    _atomic_write(data)


def set_question(ref: str, question_id: str) -> None:
    data = _load_map()
    entry = data.get(ref)
    if not isinstance(entry, dict):
        return
    entry = dict(entry)
    entry["question_id"] = question_id
    data[ref] = entry
    _atomic_write(data)


def lookup(ref: str) -> PendingLoop | None:
    data = _load_map()
    return _entry_from_raw(data.get(ref))


def find_by_loop_id(loop_id: str) -> str | None:
    """Return the ref matching ``loop_id``, or None."""
    if not loop_id:
        return None
    data = _load_map()
    for k, raw in data.items():
        entry = _entry_from_raw(raw)
        if entry is None or entry.loop_id != loop_id:
            continue
        if isinstance(k, str) and k:
            return k
    return None


def clear_question(ref: str) -> None:
    data = _load_map()
    entry = data.get(ref)
    if not isinstance(entry, dict):
        return
    entry = dict(entry)
    entry["question_id"] = None
    data[ref] = entry
    _atomic_write(data)


def unregister(ref: str) -> None:
    data = _load_map()
    if ref not in data:
        return
    del data[ref]
    _atomic_write(data)
