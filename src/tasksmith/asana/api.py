"""Stateless Asana REST helpers (client is always keyword-only)."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from tasksmith.asana.errors import AsanaAPIError


def _require_data_dict(resp: dict[str, Any], *, context: str) -> dict[str, Any]:
    data = resp.get("data")
    if not isinstance(data, dict):
        raise AsanaAPIError(f"{context}: response data is not an object")
    return data


def _require_gid(data: dict[str, Any], *, context: str) -> str:
    gid = data.get("gid")
    if not isinstance(gid, str) or not gid.strip():
        raise AsanaAPIError(f"{context}: response data has no gid")
    return gid


def get_task(
    task_gid: str,
    *,
    opt_fields: str,
    client: Any,
) -> dict[str, Any]:
    query = urlencode({"opt_fields": opt_fields})
    resp = client.request_json("GET", f"/tasks/{task_gid}", query=query)
    return _require_data_dict(resp, context="get_task")


def list_project_tasks(
    project_gid: str,
    *,
    opt_fields: str,
    offset: str | None = None,
    client: Any,
) -> tuple[list[dict[str, Any]], str | None]:
    params: dict[str, str] = {
        "opt_fields": opt_fields,
        "limit": "100",
        "completed_since": "1970-01-01T00:00:00Z",
    }
    if offset:
        params["offset"] = offset
    query = urlencode(params)
    resp = client.request_json("GET", f"/projects/{project_gid}/tasks", query=query)
    data = resp.get("data")
    if not isinstance(data, list):
        raise AsanaAPIError("list_project_tasks: response data is not a list")
    rows = [item for item in data if isinstance(item, dict)]
    next_page = resp.get("next_page")
    next_offset: str | None = None
    if isinstance(next_page, dict):
        raw = next_page.get("offset")
        if isinstance(raw, str) and raw:
            next_offset = raw
    return rows, next_offset


def create_task(
    name: str,
    *,
    project_gid: str,
    due_on: str | None,
    notes: str | None,
    html_notes: str | None,
    client: Any,
) -> str:
    body: dict[str, Any] = {"name": name, "projects": [project_gid]}
    if due_on:
        body["due_on"] = due_on
    if html_notes:
        body["html_notes"] = html_notes
    elif notes:
        body["notes"] = notes
    resp = client.request_json("POST", "/tasks", body=body)
    return _require_gid(_require_data_dict(resp, context="create_task"), context="create_task")


def create_subtask(
    parent_gid: str,
    name: str,
    *,
    due_on: str | None,
    notes: str | None,
    html_notes: str | None,
    client: Any,
) -> str:
    body: dict[str, Any] = {"name": name}
    if due_on:
        body["due_on"] = due_on
    if html_notes:
        body["html_notes"] = html_notes
    elif notes:
        body["notes"] = notes
    resp = client.request_json("POST", f"/tasks/{parent_gid}/subtasks", body=body)
    return _require_gid(_require_data_dict(resp, context="create_subtask"), context="create_subtask")


def update_task(
    task_gid: str,
    *,
    name: str | None,
    due_on: str | None,
    notes: str | None,
    html_notes: str | None,
    completed: bool | None,
    client: Any,
) -> None:
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if due_on is not None:
        body["due_on"] = due_on
    if html_notes is not None:
        body["html_notes"] = html_notes
    elif notes is not None:
        body["notes"] = notes
    if completed is not None:
        body["completed"] = completed
    if not body:
        return
    client.request_json("PUT", f"/tasks/{task_gid}", body=body)


def add_task_to_section(section_gid: str, task_gid: str, *, client: Any) -> None:
    client.request_json("POST", f"/sections/{section_gid}/addTask", body={"task": task_gid})


def get_task_stories(task_gid: str, *, client: Any) -> list[dict[str, Any]]:
    query = urlencode({"opt_fields": "gid,type,text,created_at,created_by.name"})
    resp = client.request_json("GET", f"/tasks/{task_gid}/stories", query=query)
    data = resp.get("data")
    return data if isinstance(data, list) else []


def add_comment(task_gid: str, text: str, *, client: Any) -> None:
    client.request_json("POST", f"/tasks/{task_gid}/stories", body={"text": text})


def list_workspace_tags(workspace_gid: str, *, client: Any) -> list[dict[str, Any]]:
    query = urlencode({"opt_fields": "gid,name"})
    resp = client.request_json("GET", f"/workspaces/{workspace_gid}/tags", query=query)
    data = resp.get("data")
    return data if isinstance(data, list) else []


def create_tag(workspace_gid: str, *, name: str, client: Any) -> dict[str, Any]:
    resp = client.request_json("POST", "/tags", body={"name": name, "workspace": workspace_gid})
    data = _require_data_dict(resp, context="create_tag")
    _require_gid(data, context="create_tag")
    return data


def add_tag_to_task(task_gid: str, *, tag_gid: str, client: Any) -> None:
    client.request_json("POST", f"/tasks/{task_gid}/addTag", body={"tag": tag_gid})


def remove_tag_from_task(task_gid: str, *, tag_gid: str, client: Any) -> None:
    client.request_json("POST", f"/tasks/{task_gid}/removeTag", body={"tag": tag_gid})


def get_subtasks(parent_gid: str, *, opt_fields: str, client: Any) -> list[dict[str, Any]]:
    query = urlencode({"opt_fields": opt_fields})
    resp = client.request_json("GET", f"/tasks/{parent_gid}/subtasks", query=query)
    data = resp.get("data")
    if not isinstance(data, list):
        raise AsanaAPIError("get_subtasks: response data is not a list")
    return data


def project_workspace_gid(project_gid: str, *, client: Any) -> str:
    query = urlencode({"opt_fields": "workspace.gid"})
    resp = client.request_json("GET", f"/projects/{project_gid}", query=query)
    data = _require_data_dict(resp, context="project_workspace_gid")
    ws = data.get("workspace")
    if not isinstance(ws, dict):
        raise AsanaAPIError("project_workspace_gid: workspace missing")
    gid = ws.get("gid")
    if not isinstance(gid, str) or not gid.strip():
        raise AsanaAPIError("project_workspace_gid: workspace.gid missing")
    return gid


def create_project(
    name: str,
    *,
    like_project_gid: str,
    client: Any,
) -> dict[str, Any]:
    query = urlencode({"opt_fields": "workspace.gid,team.gid"})
    ref = client.request_json("GET", f"/projects/{like_project_gid}", query=query)
    data = _require_data_dict(ref, context="create_project")
    ws = data.get("workspace")
    workspace_gid = ws.get("gid") if isinstance(ws, dict) else None
    if not workspace_gid:
        raise AsanaAPIError("create_project: workspace.gid missing")
    body: dict[str, Any] = {"name": name, "workspace": workspace_gid}
    team = data.get("team")
    if isinstance(team, dict) and team.get("gid"):
        body["team"] = team["gid"]
    resp = client.request_json("POST", "/projects", body=body)
    out = _require_data_dict(resp, context="create_project")
    return {"gid": _require_gid(out, context="create_project"), "name": str(out.get("name", name))}


def search_tasks(
    query: str,
    *,
    workspace_gid: str,
    project_gid: str,
    limit: int = 20,
    client: Any,
    opt_fields: str = "name,due_on,gid,completed",
) -> list[dict[str, Any]]:
    q = urlencode(
        {
            "text": query,
            "resource_type": "task",
            "opt_fields": opt_fields,
            "limit": str(min(limit, 100)),
        }
    )
    try:
        resp = client.request_json("GET", f"/workspaces/{workspace_gid}/tasks/search", query=q)
        data = resp.get("data")
        if isinstance(data, list):
            return data[:limit]
    except AsanaAPIError as e:
        if e.status_code not in (400, 402, 403):
            raise

    lower_q = query.lower()
    collected: list[dict[str, Any]] = []
    offset: str | None = None
    while len(collected) < 500:
        rows, offset = list_project_tasks(
            project_gid, opt_fields=opt_fields, offset=offset, client=client
        )
        for row in rows:
            name = row.get("name") or ""
            if row.get("completed"):
                continue
            if lower_q in str(name).lower():
                collected.append(row)
                if len(collected) >= limit:
                    return collected[:limit]
        if offset is None:
            break
    return collected[:limit]


def writer_display_name(*, client: Any) -> str | None:
    resp = client.request_json("GET", "/users/me")
    data = resp.get("data") if isinstance(resp, dict) else None
    name = data.get("name") if isinstance(data, dict) else None
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None
