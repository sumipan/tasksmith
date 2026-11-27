"""TaskStore implementation backed by Asana."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Literal

from tasksmith import body as body_format
from tasksmith.asana import api, html, subtasks, tags
from tasksmith.asana.client import AsanaClient, ClientRole
from tasksmith.asana.errors import AsanaAPIError, AsanaConfigError
from tasksmith.store import TaskNotFoundError

_log = logging.getLogger(__name__)

ClientFactory = Callable[[str, ClientRole], AsanaClient]

_TASK_OPT_FIELDS = (
    "name,completed,completed_at,created_at,due_on,due_at,start_on,notes,html_notes,permalink_url,"
    "assignee.name,parent.gid,parent.name,memberships.section.gid,memberships.section.name,"
    "memberships.project.gid,"
    "subtasks.name,subtasks.completed,subtasks.completed_at,subtasks.created_at,"
    "subtasks.due_on,subtasks.start_on,subtasks.notes,subtasks.html_notes,"
    "subtasks.permalink_url,subtasks.assignee.name,subtasks.parent.gid,"
    "subtasks.memberships.section.gid,subtasks.memberships.section.name,"
    "subtasks.memberships.project.gid"
)


class _CountingClient:
    def __init__(self, inner: Any, owner: AsanaTaskStore) -> None:
        self._inner = inner
        self._owner = owner

    def request_json(
        self,
        method: str,
        path: str,
        *,
        query: str | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._owner.calls += 1
        return self._inner.request_json(method, path, query=query, body=body)


def _section_gid(task: dict[str, Any], project_gid: str | None) -> str | None:
    fallback: str | None = None
    for m in task.get("memberships") or []:
        if not isinstance(m, dict):
            continue
        sec = m.get("section") or {}
        gid = str(sec.get("gid") or "").strip() if isinstance(sec, dict) else ""
        if not gid:
            continue
        proj = m.get("project") or {}
        pgid = str(proj.get("gid") or "") if isinstance(proj, dict) else ""
        if project_gid and pgid == project_gid:
            return gid
        if fallback is None:
            fallback = gid
    return fallback if not project_gid else None


def _body_fields(body: str | None) -> dict[str, str | None]:
    if body is None:
        return {"notes": None, "html_notes": None}
    if body_format.is_controlled_notes(body):
        return {"notes": None, "html_notes": html.body_to_html(body)}
    return {"notes": body, "html_notes": None}


def _gid_key(row: dict[str, Any]) -> tuple[int, str]:
    g = str(row.get("gid") or "")
    return (0, f"{int(g):020d}") if g.isdigit() else (1, g)


def _to_store_task(
    raw: dict[str, Any], project_gid: str | None = None, *, parent_gid: str | None = None
) -> dict[str, Any]:
    out = dict(raw)
    completed_at = raw.get("completed_at")
    if raw.get("completed") and isinstance(completed_at, str):
        out["completed_at"] = completed_at
    else:
        out["completed_at"] = None
    out["section_gid"] = _section_gid(raw, project_gid)
    parent = raw.get("parent")
    if isinstance(parent, dict) and parent.get("gid"):
        out["parent"] = {"gid": str(parent["gid"])}
        if parent.get("name"):
            out["parent"]["name"] = str(parent["name"])
    elif parent_gid:
        out["parent"] = {"gid": parent_gid}
    else:
        out["parent"] = None
    html_notes = out.pop("html_notes", None)
    notes = out.pop("notes", None)
    body = html.html_to_body(html_notes) if isinstance(html_notes, str) else None
    out["body"] = body if body is not None else (notes or "")
    if out.get("permalink_url") is None:
        out["permalink_url"] = ""
    if "subtasks" in raw:
        subs = [s for s in (raw.get("subtasks") or []) if isinstance(s, dict)]
        gid = str(raw.get("gid") or "") or None
        kids = [_to_store_task(s, project_gid, parent_gid=gid) for s in subs]
        for k in kids:
            k.pop("subtasks", None)
        out["subtasks"] = sorted(kids, key=_gid_key)
    return out


def _normalize_story(row: dict[str, Any]) -> dict[str, Any]:
    created_by = row.get("created_by")
    name = None
    if isinstance(created_by, dict):
        raw_name = created_by.get("name")
        if isinstance(raw_name, str):
            name = raw_name
    return {
        "gid": str(row.get("gid") or ""),
        "type": row.get("type"),
        "text": row.get("text") or "",
        "created_at": row.get("created_at"),
        "created_by": {"name": name},
    }


class AsanaTaskStore:
    """TaskStore backed by the Asana REST API."""

    def __init__(
        self,
        *,
        writer_token: str,
        reader_token: str | None = None,
        project_gid: str,
        workspace_gid: str | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        if not writer_token:
            raise AsanaConfigError("writer_token must not be empty")
        if not project_gid:
            raise AsanaConfigError("project_gid must not be empty")
        self._writer_token = writer_token
        self._reader_token = reader_token if reader_token else writer_token
        self._project_gid = project_gid
        self._workspace_gid = workspace_gid
        self._factory = client_factory or (
            lambda token, role: AsanaClient(token, role=role)
        )
        self._index_tag_cache: dict[str, str] = {}
        self._user_tag_cache: dict[str, dict[str, str]] = {}
        self._ai_names: frozenset[str] | None = None
        self.calls = 0

    def _make_client(self, role: Literal["writer", "reader"]) -> Any:
        token = self._writer_token if role == "writer" else self._reader_token
        inner = self._factory(token, role)
        return _CountingClient(inner, self)

    def _writer(self) -> Any:
        return self._make_client("writer")

    def _reader(self) -> Any:
        return self._make_client("reader")

    def _resolve_workspace_gid(self, client: Any) -> str:
        if self._workspace_gid:
            return self._workspace_gid
        self._workspace_gid = api.project_workspace_gid(self._project_gid, client=client)
        return self._workspace_gid

    def get_task(self, task_gid: str) -> dict:
        try:
            raw = api.get_task(task_gid, opt_fields=_TASK_OPT_FIELDS, client=self._reader())
        except AsanaAPIError as e:
            if e.status_code == 404:
                raise TaskNotFoundError(task_gid) from e
            raise
        return _to_store_task(raw, self._project_gid)

    def list_tasks(self, project_gid: str | None = None) -> list[dict]:
        pid = project_gid or self._project_gid
        reader = self._reader()
        rows: list[dict[str, Any]] = []
        offset: str | None = None
        while True:
            page, offset = api.list_project_tasks(
                pid, opt_fields=_TASK_OPT_FIELDS, offset=offset, client=reader
            )
            rows.extend(page)
            if offset is None:
                break
        return [_to_store_task(r, pid) for r in rows]

    def get_task_stories(self, task_gid: str) -> list[dict]:
        raw = api.get_task_stories(task_gid, client=self._reader())
        return [_normalize_story(r) for r in raw if isinstance(r, dict)]

    def create_task(
        self,
        name: str,
        *,
        project_gid: str | None = None,
        due_on: str | None = None,
        section_gid: str | None = None,
        body: str | None = None,
        assign_owner: str | None = None,
    ) -> str:
        del assign_owner
        pid = project_gid or self._project_gid
        fields = _body_fields(body)
        w = self._writer()
        gid = api.create_task(
            name,
            project_gid=pid,
            due_on=due_on,
            notes=fields["notes"],
            html_notes=fields["html_notes"],
            client=w,
        )
        if section_gid:
            api.add_task_to_section(section_gid, gid, client=w)
        self._sync_tags(gid, body, "create_task", w)
        return gid

    def create_subtask(
        self,
        parent_gid: str,
        name: str,
        *,
        project_gid: str | None = None,
        due_on: str | None = None,
        section_gid: str | None = None,
        body: str | None = None,
        assign_owner: str | None = None,
    ) -> str:
        del project_gid, assign_owner
        fields = _body_fields(body)
        w = self._writer()
        gid = api.create_subtask(
            parent_gid,
            name,
            due_on=due_on,
            notes=fields["notes"],
            html_notes=fields["html_notes"],
            client=w,
        )
        if section_gid:
            api.add_task_to_section(section_gid, gid, client=w)
        self._sync_tags(gid, body, "create_subtask", w)
        try:
            self._sort_subtasks(parent_gid, client=w)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "subtask sort failed after create_subtask parent=%s: %s", parent_gid, exc
            )
        return gid

    def update_task(
        self,
        task_gid: str,
        *,
        name: str | None = None,
        due_on: str | None = None,
        section_gid: str | None = None,
        body: str | None = None,
        completed: bool | None = None,
    ) -> None:
        fields = _body_fields(body)
        w = self._writer()
        api.update_task(
            task_gid,
            name=name,
            due_on=due_on,
            notes=fields["notes"],
            html_notes=fields["html_notes"],
            completed=completed,
            client=w,
        )
        if section_gid:
            api.add_task_to_section(section_gid, task_gid, client=w)
        self._sync_tags(task_gid, body, "update_task", w)
        if due_on is not None or completed is not None:
            try:
                self._sort_parent_of_task(task_gid, client=w)
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "subtask sort failed after update_task task=%s: %s", task_gid, exc
                )

    def add_comment(self, task_gid: str, text: str) -> None:
        api.add_comment(task_gid, text, client=self._writer())

    def ai_actor_names(self) -> frozenset[str]:
        if self._ai_names is not None:
            return self._ai_names
        try:
            name = api.writer_display_name(client=self._writer())
        except Exception:  # noqa: BLE001
            self._ai_names = frozenset()
            return self._ai_names
        self._ai_names = frozenset({name}) if name else frozenset()
        return self._ai_names

    def _sync_tags(self, gid: str, body: str | None, op: str, client: Any) -> None:
        if body is None:
            return
        try:
            workspace_gid = self._resolve_workspace_gid(client)
            tags.sync_index_tags(
                gid,
                body,
                workspace_gid=workspace_gid,
                client=client,
                index_cache=self._index_tag_cache,
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("index tag sync failed after %s task=%s: %s", op, gid, exc)
        try:
            workspace_gid = self._resolve_workspace_gid(client)
            tags.sync_user_tags(
                gid,
                body,
                workspace_gid=workspace_gid,
                client=client,
                user_cache=self._user_tag_cache,
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("user tag sync failed after %s task=%s: %s", op, gid, exc)

    def _sort_subtasks(self, parent_gid: str, *, client: Any) -> None:
        rows = api.get_subtasks(
            parent_gid, opt_fields="gid,name,due_on,completed", client=client
        )
        current = subtasks.list_subtask_gids(rows)
        target = subtasks.plan_order(rows)
        moves = subtasks.plan_moves(current, target)
        subtasks.apply_order(parent_gid, moves, client=client)

    def _sort_parent_of_task(self, task_gid: str, *, client: Any) -> None:
        task = api.get_task(task_gid, opt_fields="parent.gid", client=client)
        parent = task.get("parent")
        if not isinstance(parent, dict) or not parent.get("gid"):
            return
        self._sort_subtasks(str(parent["gid"]), client=client)
