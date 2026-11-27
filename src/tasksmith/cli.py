"""tasksmith CLI: reconcile, tick, human commands, finish-node."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from dataclasses import asdict
from pathlib import Path

from tasksmith.execution import GhdagExecutor, GhdagThinker, set_executor, set_thinker
from tasksmith.external import new_external_body, parse_check_at
from tasksmith.file_channel import FileChannel
from tasksmith.human import record_human_reply, request_human
from tasksmith.intake import register_items, seen_ids, unseen
from tasksmith.local import LocalTaskStore
from tasksmith.reconcile import ReconcileResult, _load_sections_config, reconcile
from tasksmith.runtime import set_human_channel, set_task_store, task_store
from tasksmith.settings import Settings, set_settings, settings
from tasksmith.store import TaskStoreError
from tasksmith.tags import summarize
from tasksmith.tick import (
    SectionAssignment,
    _section_counts,
    adopt_completions,
    aggregate_metrics,
    assign_sections,
    finish_node,
    format_metrics_text,
    recover,
    tick,
)

__all__ = ["main"]


def _parse_routes(route_args: list[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for spec in route_args:
        if ":" in spec:
            engine, model = spec.split(":", 1)
        else:
            engine, model = spec, ""
        out.append((engine.strip(), model.strip()))
    return out


def _install_root(
    root: Path,
    *,
    routes: list[tuple[str, str]],
    queue_dir: Path | None,
) -> None:
    root = root.resolve()
    tasks = root / ".tasks"
    jobs = queue_dir if queue_dir is not None else root / "jobs"
    state = root / ".pipeline-state"
    set_task_store(LocalTaskStore(root))
    set_human_channel(FileChannel(tasks))
    set_settings(
        Settings(
            state_dir=tasks / "state",
            project_id=None,
            executor_exists=lambda _name: True,
            request_channel_id=str(tasks),
            timezone="UTC",
        )
    )
    if routes:
        quoted_root = shlex.quote(str(root))
        finish_template = (
            f"python3 -m tasksmith --root {quoted_root} finish-node "
            "${task_id} --result ${queue_dir}/${llm_result_filename} "
            "--meta-digest ${meta_digest}"
        )
        set_executor(
            GhdagExecutor(
                queue_dir=jobs,
                state_dir=state,
                route=routes,
                finish_template=finish_template,
            )
        )
        set_thinker(
            GhdagThinker(
                queue_dir=jobs,
                state_dir=state,
                routes={"light": routes, "heavy": routes},
            )
        )


def _resolve_root(args: argparse.Namespace) -> Path | None:
    raw = args.root or os.environ.get("TASKSMITH_ROOT", "").strip()
    return Path(raw).resolve() if raw else None


def _maybe_install_from_args(args: argparse.Namespace) -> list[tuple[str, str]]:
    routes = _parse_routes(args.route or [])
    root = _resolve_root(args)
    if root is not None:
        qd = Path(args.queue_dir).resolve() if args.queue_dir else None
        _install_root(root, routes=routes, queue_dir=qd)
    return routes


def _require_route(command: str, routes: list[tuple[str, str]]) -> int | None:
    if command not in ("tick", "record-human-reply") or routes:
        return None
    from tasksmith.execution import executor as executor_fn
    from tasksmith.execution import thinker as thinker_fn

    missing_executor = False
    missing_thinker = False
    try:
        executor_fn()
    except RuntimeError:
        missing_executor = True
    try:
        thinker_fn()
    except RuntimeError:
        missing_thinker = True
    if command == "record-human-reply" and missing_thinker:
        print(f"error: --route is required for {command}", file=sys.stderr)
        return 2
    if command == "tick" and missing_executor and missing_thinker:
        print(f"error: --route is required for {command}", file=sys.stderr)
        return 2
    return None


def result_to_dict(result: ReconcileResult) -> dict:
    d = asdict(result)
    d.pop("section_notes", None)
    for value in d.values():
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    item.pop("body", None)
    return d


def _format_reconcile_text(result: ReconcileResult) -> str:
    lines = [
        f"ready={len(result.ready)} review={len(result.review)} "
        f"waiting_human={len(result.waiting_human)} blocked={len(result.blocked)} "
        f"drift={len(result.drift)}"
    ]
    if result.error is not None:
        lines.append(f"error={result.error}")
    for node in result.ready:
        lines.append(f"  ready: {node.task_gid} {node.name!r}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tasksmith")
    parser.add_argument("--root", default=None, help="local task store root (or TASKSMITH_ROOT)")
    parser.add_argument(
        "--queue-dir",
        default=None,
        help="ghdag queue directory (default: <root>/jobs)",
    )
    parser.add_argument(
        "--route",
        action="append",
        default=None,
        help="engine[:model] route (repeatable; required for tick / record-human-reply without host wiring)",
    )
    parser.add_argument(
        "command",
        choices=(
            "reconcile",
            "tick",
            "section",
            "adopt",
            "finish-node",
            "recover",
            "request-human",
            "record-human-reply",
            "metrics",
            "intake",
            "intake-seen",
            "tags",
            "external",
        ),
    )
    parser.add_argument("task_gid", nargs="?", default=None)
    parser.add_argument("--result", dest="result_path", default=None)
    parser.add_argument("--meta-digest", dest="meta_digest", default=None)
    parser.add_argument("--project", dest="project_gid", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max", dest="max_running", type=int, default=None)
    parser.add_argument("--max-human", dest="max_human", type=int, default=3)
    parser.add_argument("--max-review", dest="max_review", type=int, default=1)
    parser.add_argument("--max-rollup", dest="max_rollup", type=int, default=5)
    parser.add_argument("--max-adopt", dest="max_adopt", type=int, default=10)
    parser.add_argument("--max-section", dest="max_section", type=int, default=5)
    parser.add_argument("--max-external", dest="max_external", type=int, default=3)
    parser.add_argument("--title", default=None)
    parser.add_argument("--check", dest="external_check", default=None)
    parser.add_argument("--at", dest="at_iso", default=None)
    parser.add_argument("--in", dest="in_minutes", type=int, default=None)
    parser.add_argument("--every", dest="check_every", type=int, default=60)
    parser.add_argument("--window", dest="check_window", default=None)
    parser.add_argument("--param", action="append", default=[])
    parser.add_argument("--report-to", dest="report_to", default=None)
    parser.add_argument("--message", default=None)
    parser.add_argument("--ref", default=None)
    parser.add_argument("--items", default=None, help="JSON file of intake items (intake)")
    parser.add_argument("--reference", default=None, help="reference text for intake (intake)")
    parser.add_argument("--tag", action="append", default=None, help="tag name (intake; repeatable)")
    parser.add_argument(
        "--unseen",
        action="append",
        default=None,
        dest="unseen_ids",
        help="source ID to filter as not yet seen (intake-seen; repeatable)",
    )
    parser.add_argument("--finished-since", dest="finished_since", default=None)
    parser.add_argument("--finished-until", dest="finished_until", default=None)
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args(argv)

    if args.command == "section":
        if args.max_running is None:
            args.max_running = 200
        elif args.max_running < 0:
            parser.error("--max must be 0 or greater")
    elif args.command == "external":
        if args.max_running is None:
            args.max_running = 5
        elif args.max_running < 1:
            parser.error("--max must be at least 1")
    elif args.max_running is None:
        args.max_running = 1

    routes = _maybe_install_from_args(args)
    root = _resolve_root(args)
    if root is None:
        try:
            task_store()
        except RuntimeError:
            print("error: --root or TASKSMITH_ROOT is required", file=sys.stderr)
            return 2

    route_err = _require_route(args.command, routes)
    if route_err is not None:
        return route_err

    try:
        if args.command == "metrics":
            if args.days < 1:
                print("error: --days must be at least 1", file=sys.stderr)
                return 1
            summary = aggregate_metrics(days=args.days)
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                print(format_metrics_text(summary))
            return 0

        if args.command == "intake":
            if not args.items or not args.tag:
                print("error: intake requires --items and at least one --tag", file=sys.stderr)
                return 2
            items = json.loads(Path(args.items).read_text(encoding="utf-8"))
            result = register_items(
                items,
                project_id=args.project_gid,
                tags=args.tag,
                store=task_store(),
                reference=args.reference,
            )
            print(json.dumps(asdict(result), ensure_ascii=False, indent=2 if args.json else None))
            return 0 if not result.failed else 1

        if args.command == "intake-seen":
            if args.unseen_ids:
                ids = unseen(args.unseen_ids)
                if args.json:
                    print(json.dumps(ids, ensure_ascii=False))
                else:
                    for sid in ids:
                        print(sid)
            else:
                count = len(seen_ids())
                if args.json:
                    print(json.dumps({"count": count}, ensure_ascii=False))
                else:
                    print(count)
            return 0

        if args.command == "tags":
            from datetime import datetime, timezone

            groups = summarize(task_store().list_tasks(args.project_gid))
            if args.finished_since is not None or args.finished_until is not None:

                def _parse_instant(raw: str) -> datetime:
                    text = raw.strip()
                    if text.endswith("Z"):
                        text = f"{text[:-1]}+00:00"
                    dt = datetime.fromisoformat(text)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt

                try:
                    since = (
                        _parse_instant(args.finished_since)
                        if args.finished_since is not None
                        else None
                    )
                    until = (
                        _parse_instant(args.finished_until)
                        if args.finished_until is not None
                        else None
                    )
                except ValueError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    return 2
                filtered: list = []
                for group in groups:
                    if not group.finished or group.finished_at is None:
                        continue
                    try:
                        at = _parse_instant(group.finished_at)
                    except ValueError:
                        print(f"error: invalid finished_at on tag {group.tag!r}", file=sys.stderr)
                        return 2
                    if since is not None and at < since:
                        continue
                    if until is not None and at >= until:
                        continue
                    filtered.append(group)
                groups = filtered

            if args.json:
                print(
                    json.dumps(
                        [asdict(g) for g in groups],
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            else:
                for group in groups:
                    print(
                        f"{group.tag} open={len(group.open)} "
                        f"done={len(group.done)} finished={group.finished}"
                    )
            return 0

        if args.command == "external":
            if args.task_gid != "add":
                print("error: external requires add", file=sys.stderr)
                return 2
            if not args.title or not args.external_check:
                print("error: external add requires --title and --check", file=sys.stderr)
                return 2
            if (args.at_iso is None) == (args.in_minutes is None):
                print("error: external add requires exactly one of --at or --in", file=sys.stderr)
                return 2
            params: dict[str, str] = {}
            for spec in args.param:
                if "=" not in spec:
                    print("error: --param requires key=value", file=sys.stderr)
                    return 2
                key, val = spec.split("=", 1)
                params[key] = val
            st = settings()
            from datetime import datetime, timedelta
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(st.timezone)
            if args.in_minutes is not None:
                check_at = datetime.now(tz) + timedelta(minutes=args.in_minutes)
            else:
                try:
                    check_at = parse_check_at(args.at_iso)
                except ValueError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    return 2
            body = new_external_body(
                objective=args.title,
                check=args.external_check,
                check_at=check_at,
                check_every=args.check_every,
                check_max=args.max_running,
                check_window=args.check_window,
                params=params or None,
                report_to=args.report_to,
            )
            gid = task_store().create_task(
                args.title,
                body=body,
                project_gid=args.project_gid,
            )
            print(gid)
            return 0

        if args.command == "reconcile":
            result = reconcile(args.project_gid)
            if args.json:
                print(json.dumps(result_to_dict(result), ensure_ascii=False, indent=2))
            else:
                print(_format_reconcile_text(result))
            return 1 if result.error is not None else 0

        if args.command == "section":
            rec = reconcile(args.project_gid)
            config = _load_sections_config()
            if config is None:
                print("assign_sections skipped: sections config missing", file=sys.stderr)
                assignments: list[SectionAssignment] = []
                warns: list[str] = []
            else:
                warns = []
                assignments = assign_sections(
                    rec,
                    config=config,
                    max_section=args.max_running,
                    dry_run=args.dry_run,
                    warnings=warns,
                )
            sec_counts = _section_counts(assignments)
            sec_counts["section_unresolved"] = len(rec.section_unresolved)
            if args.json:
                print(
                    json.dumps(
                        {"assignments": [asdict(a) for a in assignments], "counts": sec_counts},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            else:
                for a in assignments:
                    print(f"{a.task_gid} {a.outcome} {a.section_id or '-'} {a.name}")
                for w in warns:
                    print(w)
            return 0

        if args.command == "adopt":
            rec = reconcile(args.project_gid)
            ar = adopt_completions(rec, max_adopt=args.max_adopt, dry_run=args.dry_run)
            if args.json:
                print(json.dumps(asdict(ar), ensure_ascii=False, indent=2))
            else:
                print(
                    f"{'dry-run ' if ar.dry_run else ''}adopted={len(ar.adopted)}"
                    f" reopened={len(ar.reopened)} errors={len(ar.errors)}"
                )
            return 0 if not ar.errors else 1

        if args.command == "tick":
            tr = tick(
                args.project_gid,
                dry_run=args.dry_run,
                max_running=args.max_running,
                max_human=args.max_human,
                max_review=args.max_review,
                max_rollup=args.max_rollup,
                max_adopt=args.max_adopt,
                max_section=args.max_section,
                max_external=args.max_external,
            )
            if args.json:
                print(
                    json.dumps(
                        {
                            "status": tr.status,
                            "task_gid": tr.task_gid,
                            "name": tr.name,
                            "message": tr.message,
                            "request_text": tr.request_text,
                            "reminded_gids": list(tr.reminded_gids),
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            else:
                print(tr.message)
            return 0

        if args.command in (
            "finish-node",
            "recover",
            "request-human",
            "record-human-reply",
        ) and not args.task_gid:
            print(f"error: {args.command} requires task_gid", file=sys.stderr)
            return 1

        if args.command == "finish-node":
            if not args.result_path or not args.meta_digest:
                print("error: finish-node requires --result and --meta-digest", file=sys.stderr)
                return 1
            rr = finish_node(
                args.task_gid,
                result_path=Path(args.result_path),
                meta_digest=args.meta_digest,
            )
            if args.json:
                print(json.dumps(asdict(rr), ensure_ascii=False, indent=2))
            else:
                print(f"{rr.status}: {rr.reason} (gid={rr.task_gid})")
            return 0

        if args.command == "request-human":
            hr = request_human(
                args.task_gid,
                project_gid=args.project_gid,
                dry_run=args.dry_run,
            )
            if args.json:
                print(json.dumps(asdict(hr), ensure_ascii=False, indent=2))
            else:
                print(hr.request_text)
            return 0

        if args.command == "record-human-reply":
            if not args.message or not args.ref:
                print("error: record-human-reply requires --message / --ref", file=sys.stderr)
                return 1
            rr = record_human_reply(
                args.task_gid,
                args.message,
                ref=args.ref,
                project_gid=args.project_gid,
                dry_run=args.dry_run,
            )
            if args.json:
                print(json.dumps(asdict(rr), ensure_ascii=False, indent=2))
            else:
                print(f"{rr.decision} -> {rr.transitioned_to}")
            return 0

        rec = recover(args.task_gid, project_gid=args.project_gid)
        if args.json:
            print(
                json.dumps(
                    {"status": rec.status, "task_gid": rec.task_gid, "message": rec.message},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(rec.message)
        return 0 if rec.status == "recovered" else 1
    except (TaskStoreError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
