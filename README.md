# tasksmith

tasksmith is a coordinator that moves work forward when humans and AI agents are nodes in the same task DAG. The task body in the fixed `tasksmith/v1` format is the source of truth. State transitions are deterministic (`tick` decides what runs next). AI nodes are handed off to an `Executor`. Human nodes go through a `HumanChannel` that asks and waits for a reply.

## Install

```bash
pip install "ghdag @ git+https://github.com/sumipan/ghdag.git@main"
pip install -e ".[dev]"
```

Python 3.12 or newer. The only runtime dependency is `ghdag`. `pyproject.toml` declares only a lower bound (`ghdag>=0.152.2`) and no source, so install ghdag first; the host that uses tasksmith decides which ghdag version to install.

## Quick start

Use an empty directory as `--root` and run the steps below in order. Task creation is short Python (there is no CLI command to create tasks).

```python
from pathlib import Path

from tasksmith import LocalTaskStore
from tasksmith.body import build_controlled_notes, update_meta_field
from tasksmith.model import OwnerType, PipelineState

root = Path("/tmp/tasksmith-demo")
store = LocalTaskStore(root)

body_human = build_controlled_notes(
    objective="Approve downstream AI work",
    owner=OwnerType.HUMAN,
    state=PipelineState.READY,
    flow="dag",
    acceptance=["Human reply is recorded"],
)
gid_human = store.create_task("human gate", body=body_human)

body_ai = build_controlled_notes(
    objective="Run demo skill",
    owner=OwnerType.AI,
    state=PipelineState.READY,
    flow="dag",
    executor="skill:demo",
    deliverables="- output.txt",
    acceptance=["Result has PIPELINE_STATUS: DONE"],
)
body_ai = update_meta_field(body_ai, "depends_on", gid_human, create=True)
store.create_task("ai node", body=body_ai)
```

See `tests/test_local_loop.py` for a full in-process loop with injected executor and thinker.

```bash
python -m tasksmith --root /tmp/tasksmith-demo reconcile
```

```
ready=1 review=0 waiting_human=0 blocked=1 drift=0
  ready: 1 'human gate'
```

```bash
python -m tasksmith --root /tmp/tasksmith-demo tick
```

```
error: --route is required for tick
```

(exit code 2)

```bash
python -m tasksmith --root /tmp/tasksmith-demo --route claude tick
```

```
requested_human gid=1 channel=1 ts=None
```

```bash
cat /tmp/tasksmith-demo/.tasks/requests/1.md
```

```
[tasksmith request] human gate

## Objective / deliverables
Approve downstream AI work

## Acceptance criteria
- [ ] Human reply is recorded
...
```

```bash
python -m tasksmith --root /tmp/tasksmith-demo --route claude record-human-reply 1 --message ok --ref 1
```

`record-human-reply` always calls an LLM to interpret the reply: `GhdagThinker` places a job under `<dir>/jobs`, and tasksmith waits up to 600 seconds for a ghdag runner to execute it. You need a runner and a working LLM route.

After the human node is `DONE`, run `tick` again: the AI node moves to `RUNNING` and a job appears under `<dir>/jobs`. When the runner finishes the job, it invokes `finish-node` (with `--result` and `--meta-digest` embedded in the job by the CLI) and the node becomes `DONE`.

```bash
python -m tasksmith --root /tmp/tasksmith-demo reconcile
```

```
ready=0 review=0 waiting_human=0 blocked=0 drift=0
```

(after the full human and AI loop completes)

**External wait demo** — use another empty directory. No runner or LLM is required; `tick` must still pass `--route`, but EXTERNAL probes do not enqueue ghdag jobs.

```bash
python -m tasksmith --root /tmp/tasksmith-wait external add --title "wait demo" --check time --in 0 --param text=hello
```

```
1
```

```bash
python -m tasksmith --root /tmp/tasksmith-wait --route claude tick
```

```
external gid=1 done
```

```bash
cat /tmp/tasksmith-wait/.tasks/announcements.md
```

```
hello
```

Task `1` ends in `state: DONE`.

## Concepts

**Pipeline states** (`model.PipelineState`): `READY`, `RUNNING`, `WAITING_HUMAN`, `REVIEW`, `DONE`, `BLOCKED`. (`NORMALIZED` is a normalize-step marker, not a state-machine state.)

**Owners** (`model.OwnerType`): `HUMAN`, `AI`, `EXTERNAL`.

**External nodes** wait on an outside condition instead of an executor or a human reply. During `tick`, tasksmith probes nodes that are `flow: dag`, `state: READY`, and `owner: EXTERNAL` once `check_at` has passed.

- **Built-in probes** (`external.BUILTIN_PROBES`): `time` marks the node `DONE` when due; the `check_params` key `text` becomes the announcement body. `file_exists` completes when `check_params.path` exists (`~` is expanded).
- **Host probes**: `Settings(probes={"name": probe})` where `probe(task: dict, params: dict[str, str]) -> ProbeResult`. `ProbeResult.status` is `done` (complete), `continue` (poll again), or `speak` (announce `report`, then poll again). Names in `Settings.probes` override built-ins with the same name (`external.resolve_probe`).
- **Control metadata** (`external.new_external_body`): `check`, `check_at` (`YYYY-MM-DDTHH:MM`; no offset uses `Settings.timezone`), `check_every` (minutes, default 60), `check_max` (default 5), optional `check_window` (`HH:MM-HH:MM`, midnight wrap allowed), optional `check_params` (JSON object of strings), optional `report_to`, and `_checks` (attempt count, written by tasksmith). Invalid values or unknown `check` names are sent to `REVIEW` by the design gate on `tick` (`external.external_violations`; rule ids `design_doc.external_check_unknown` / `design_doc.external_meta_invalid`).
- **Scheduling**: Outside `check_window`, `check_at` moves to the next window opening. During `Settings.quiet_hours`, probes are skipped. Each `tick` runs at most `--max-external` nodes (default 3). Non-`done` outcomes and probe errors count as one check; when `_checks` reaches `check_max`, the node becomes `BLOCKED`.
- **Notifications**: With `report_to`, `HumanChannel.follow_up(report_to, text)`; otherwise `HumanChannel.announce(text)` (the bundled `FileChannel` appends to `.tasks/announcements.md`). Default completion text is `External task <name> completed`; exhaustion is `External task <name> exhausted after <n> checks` (`language_en`).

**Body shape**: eight sections — `Objective`, `Deliverables`, `Procedure and constraints`, `Acceptance criteria`, `Open decisions`, `Working draft`, `References`, `Control metadata` (headings come from the bundled English pack). In `Control metadata`, common fields include `format: tasksmith/v1`, `state`, `owner`, `flow`, `executor`, `inputs`, `outputs`, and `depends_on`. Fields whose names start with `_` are internal and written by tasksmith. Humans may edit `EDITABLE_SECTIONS` only: `Working draft` and `Acceptance criteria` (`body.EDITABLE_SECTIONS`).

**Dependencies**: a node is not ready until every task id listed in `depends_on` is `DONE`.

## todo.md

Tasks are canonical; `todo.md` is a human-facing view.

- **Line shape**: `- [ ] <text>` (open) or `- [x] <text>` (checked). Other lines are ignored.
- **Mark**: registered lines gain `<!-- task:<id> -->` at the end of the line.
- **Candidates** (`todo candidates` / `todo.candidates`): open, unmarked lines as a JSON array of `line_id` (first 12 hex chars of SHA-1 of the full line), `text`, and `line_no`.
- **Register** (`todo register PATH --decisions FILE` / `todo.register`): decisions are `[{"line_id": "...", "register": true|false, "reason": "...", "owner": "AI"|"HUMAN", "section": "..."}]`. Lines with `register: true` create a task in `state: REVIEW` (`HUMAN` uses `flow: personal`, otherwise `flow: dag`; default owner `AI`), stamp the line, and rewrite `todo.md` atomically. Output fields: `registered`, `skipped`, `failed`, `skipped_reasons`, `failures`.
- **Drift** (`todo drift` / `todo.find_drift`): compare marked lines with tasks; report `task_missing`, `checked_in_todo`, `unchecked_in_todo`, or `text_changed`. **Read-only** — never writes `todo.md` or tasks.

## Ports

Four ports define how tasksmith talks to storage, humans, execution, and LLM interpretation. Each has a bundled implementation and can be replaced.

| Port | Protocol | Bundled implementation | Install / replace |
|------|----------|------------------------|-------------------|
| Task store | `tasksmith.TaskStore` — `get_task`, `list_tasks`, `get_task_stories`, `create_task`, `create_subtask`, `update_task`, `add_comment`, `ai_actor_names` | `LocalTaskStore` | `tasksmith.runtime.set_task_store` (default: `TASKSMITH_ROOT` → `LocalTaskStore`) |
| Human channel | `tasksmith.human.HumanChannel` — `ask`, `follow_up`, `announce`, `link` | `tasksmith.file_channel.FileChannel` | `tasksmith.runtime.set_human_channel` |
| Executor | `tasksmith.execution.Executor` — `used`, `submit`, `failure` | `GhdagExecutor` | `tasksmith.execution.set_executor` |
| Thinker | `tasksmith.execution.Thinker` — `ask` | `GhdagThinker` | `tasksmith.execution.set_thinker` |

Hosts also call `tasksmith.settings.set_settings(Settings(...))` — including `Settings.probes` for custom EXTERNAL probes (see **External nodes** above) — and may attach `tasksmith.events.EventSink` listeners. The CLI `--root` wires these four ports plus `Settings` with `LocalTaskStore`, `FileChannel`, and the ghdag-backed executor and thinker.

## On-disk layout

Under `<root>/.tasks/`:

| Path | Content |
|------|---------|
| `tasks/<id>.json` | One task (`gid`, `name`, `body`, `completed`, `due_on`, `project`, `section`, `parent`, `assignee`, `created_at`, `modified_at`, …) |
| `tasks/<id>.stories.jsonl` | Append-only comment/system stories |
| `counter` | Last allocated numeric id |
| `lock` | Exclusive lock file while writers run |
| `requests/<n>.md` | Human request text |
| `announcements.md` | Channel announcements |
| `channel.lock` | Channel writer lock |
| `state/pending.json` | Pending human interactions |
| `state/metrics.jsonl` | Metrics events |

CLI commands with `--route` also use `<root>/jobs/` (ghdag queue; override with `--queue-dir`) and `<root>/.pipeline-state/`.

Ids are decimal strings allocated from `counter` with an `O_CREAT|O_EXCL` lock so multiple processes can share one root safely.

## CLI

```
python -m tasksmith [--root DIR] [--queue-dir DIR] [--route ENGINE[:MODEL]]... COMMAND
```

| Command | Purpose |
|---------|---------|
| `reconcile` | Scan tasks and report ready, review, waiting human, blocked, and drift buckets. |
| `tick` | Advance the pipeline (enqueue AI work, request humans, run EXTERNAL probes). **Requires `--route`.** `--max-external` (default 3) caps probes per tick. |
| `external add` | Create an EXTERNAL task. Requires `--title` and `--check`; exactly one of `--at ISO` or `--in MINUTES`; optional `--every`, `--max` (`check_max`, default 5), `--window`, repeated `--param key=value`, `--report-to`, `--project`. Prints the new task id. |
| `todo candidates` | List open unmarked lines in `todo.md` (`PATH`, JSON). |
| `todo register` | Register lines as tasks (`PATH --decisions FILE`, JSON result). |
| `todo drift` | Report todo vs task drift (`PATH`, JSON; no writes). |
| `section` | Assign section ids from host rules (`--max` default 200). |
| `adopt` | Adopt store completion flags into pipeline state. |
| `finish-node` | Apply a job result to a task (`task_gid --result PATH --meta-digest DIGEST`). |
| `recover` | Retry a stuck AI node (`task_gid`). |
| `request-human` | Send a human request for a task (`task_gid`). |
| `record-human-reply` | Record and interpret a human reply (`task_gid --message TEXT --ref REF`). **Requires `--route`.** |
| `metrics` | Print rolling metrics (`--days`, default 7). |

Shared flags: `--json`, `--dry-run`, `--project`.

## Public API

Only the modules and names below are public. Names not listed here, and any module without a listing, are not part of the stable API.

| Module | Names |
|--------|-------|
| `tasksmith` | `LocalTaskStore`, `TaskNotFoundError`, `TaskStore`, `TaskStoreError` |
| `tasksmith.body` | `EDITABLE_SECTIONS`, `FORMAT_MARKER`, `FORMAT_VERSION`, `build_controlled_notes`, `format_version`, `is_controlled_notes`, `new_controlled_notes`, `parse_meta_field`, `parse_sections`, `parse_start_on`, `remove_meta_field`, `replace_editable_sections`, `section_items`, `serialize_sections`, `set_start_on`, `update_meta_field` |
| `tasksmith.cli` | `main` |
| `tasksmith.events` | `EVENT_KINDS`, `EventKind`, `EventSink`, `NullSink`, `TasksmithEvent`, `first_line`, `safe_emit`, `utc_now_iso` |
| `tasksmith.execution` | `EngineFailed`, `EngineUnavailable`, `Executor`, `GhdagExecutor`, `GhdagThinker`, `Thinker`, `executor`, `set_executor`, `set_thinker`, `thinker` |
| `tasksmith.external` | `BUILTIN_PROBES`, `Probe`, `ProbeResult`, `external_violations`, `format_check_at`, `in_window`, `new_external_body`, `next_window_start`, `parse_check_at`, `parse_check_params`, `resolve_probe` |
| `tasksmith.gates` | `AC_MAX_COUNT`, `AC_MIN_COUNT`, `check_design_doc`, `format_violations_comment` |
| `tasksmith.human` | `HumanChannel`, `RequestHumanResult`, `RequestReviewResult`, `RecordHumanReplyResult`, `build_human_request_text`, `request_human`, `build_review_request_text`, `request_review`, `post_review_digest`, `collect_human_reply_violations`, `record_human_reply`, `remind_waiting_humans`, `parse_judgment_choices` |
| `tasksmith.language` | `LanguagePack`, `SECTION_IDS`, `language`, `language_installed`, `set_language` |
| `tasksmith.language_en` | `EN` |
| `tasksmith.model` | `FLOW_DAG`, `FLOW_PERSONAL`, `OwnerType`, `PipelineState`, `Violation` |
| `tasksmith.reconcile` | `ReconcileNode`, `ReconcileResult`, `reconcile` |
| `tasksmith.runtime` | `human_channel`, `human_channel_installed`, `set_human_channel`, `set_task_store`, `task_store` |
| `tasksmith.sections` | `SectionDef`, `SectionsConfig`, `classify_section`, `evaluate_classification`, `sections_config_from_mapping` |
| `tasksmith.settings` | `Settings`, `set_settings`, `settings`, `settings_installed` |
| `tasksmith.tick` | `AdoptResult`, `FinishNodeResult`, `RecoverResult`, `SectionAssignment`, `TickResult`, `adopt_completions`, `aggregate_metrics`, `assign_sections`, `finish_node`, `format_metrics_text`, `recover`, `tick` |
| `tasksmith.todo` | `RegisterResult`, `candidates`, `find_drift`, `line_id`, `register` |
| `tasksmith.transition` | `RollupResult`, `pipeline_status_comment`, `plan_parent_due`, `rollup_parent_due`, `rollup_parent_of_task`, `transition_task`, `validate_subtask_row` |
| `tasksmith.file_channel` | `FileChannel` |

## Language packs

The bundled pack is English (`tasksmith.language_en.EN`). Hosts provide other languages by building a `LanguagePack` and calling `tasksmith.language.set_language(pack)` (`None` resets to English). A pack controls section headings and the strings tasksmith writes in comments and violation messages.