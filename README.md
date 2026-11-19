# tasksmith

Storage layer for a task DAG orchestrator. This repository holds a **store port**
(`TaskStore`) and a **local file implementation** (`LocalTaskStore`). Orchestration
logic (state machines, document formats, ticks) lives in consuming applications;
external task backends (for example a hosted task API) are implemented there as well.

## Install

```bash
pip install -e ".[dev]"
```

Python 3.12 or newer. Runtime dependencies are stdlib-only.

## LocalTaskStore example

```python
from pathlib import Path

from tasksmith import LocalTaskStore

root = Path("/tmp/my-project")
store = LocalTaskStore(root)

task_id = store.create_task("Draft spec", project_gid="proj-1", due_on="2026-12-01")
store.add_comment(task_id, "Started from minutes.")
print(store.get_task(task_id)["name"])
```

## On-disk layout

Under `<root>/.tasks/`:

| Path | Content |
|------|---------|
| `tasks/<id>.json` | One task record (`gid`, `name`, `notes`, `html_notes`, `completed`, tags, parent, timestamps, …) |
| `tasks/<id>.stories.jsonl` | Append-only comment/system stories |
| `tags.json` | Workspace tag list `[{"gid", "name"}, …]` |
| `counter` | Last allocated numeric id |
| `lock` | Exclusive lock file while writers run |

Ids are decimal strings allocated from `counter` with an `O_CREAT|O_EXCL` lock so
multiple processes can share one root safely.

## TaskStore operations

The `TaskStore` protocol exposes:

- `get_task`, `list_tasks`, `get_task_stories`
- `create_task`, `create_subtask`, `update_task`, `add_comment`
- `list_workspace_tags`, `create_tag`, `add_tag_to_task`, `remove_tag_from_task`
- `ai_actor_names`, `default_client`

See `src/tasksmith/store.py` for argument and return shapes.