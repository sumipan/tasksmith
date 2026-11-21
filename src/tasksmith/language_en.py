"""Built-in English language pack for tasksmith."""

from __future__ import annotations

from tasksmith.language import LanguagePack

__all__ = ["EN"]

_HEADINGS = {
    "objective": "Objective",
    "deliverables": "Deliverables",
    "procedure": "Procedure and constraints",
    "acceptance": "Acceptance criteria",
    "judgments": "Open decisions",
    "draft": "Working draft",
    "references": "References",
    "control": "Control metadata"
}

_MESSAGES = {
    "adopt.done_detail": 'Adopted DONE state because the task was marked complete in the task store',
    "adopt.reopen_detail": 'Adopted state because the task was reopened as incomplete in the task store',
    "body_format.checklist_empty": 'Not started',
    "common.note_suffix": '({note})',
    "control_plane.gate_violation": 'check_design_doc violations; READY -> REVIEW:\n{listing}',
    "create_spec.dry_run_planned": 'dry-run: Spec creation planned',
    "create_spec.empty_request": 'request is empty',
    "create_spec.verify_failed": 'Did not create parent task because verify failed',
    "decompose.children_review_detail": 'decompose: created children in REVIEW (open decisions remain). keys={keys}',
    "decompose.done_detail": 'decompose done: {count} children READY → parent RUNNING',
    "decompose.dry_run_planned": 'dry-run: child creation planned',
    "decompose.key_mismatch": 'Existing child decompose_key does not match the new plan',
    "decompose.parent_deliverables_empty": 'Parent deliverables are empty',
    "decompose.parent_judgments_nonempty": 'Will not decompose because parent open decisions are non-empty',
    "decompose.parent_not_fixed_format": 'Parent is not fixed format (tasksmith/v1)',
    "decompose.parent_not_review": 'Parent state is not REVIEW (current: {state})',
    "decompose.parent_not_v1": 'Parent is not tasksmith/v1',
    "decompose.partial_failure_detail": 'decompose failed partway: {error}\nCreated: {keys}',
    "decompose.unrelated_children": 'Will not decompose because unrelated existing children remain',
    "decompose.verify_failed_children": 'Did not create children because verify failed',
    "digest.inverted_count": 'Inverted dates: {count}',
    "digest.inverted_heading": '## Tasks with inverted dates',
    "digest.inverted_item": '- {name}: start {start_s} > due {due_s} → set due on or after start',
    "digest.max_stale": 'Max REVIEW stale days: {max_stale}',
    "digest.overdue_count": 'overdue (managed): {count}',
    "digest.review_count": 'REVIEW: {count}',
    "digest.skip_before": 'skip digest: before digest_at={digest_at}',
    "digest.skip_no_channel": 'skip digest: request_channel_id not set',
    "digest.stale_suffix": ' stale {stale}d',
    "digest.title": '[tasksmith daily digest] {date}',
    "drift.notes_changed_by_non_ai": 'Body updated by non-AI: {name}',
    "elaborate.audit_detail": 'elaborate: filled executor / deliverables / procedure / acceptance',
    "elaborate.comment_ready": (
        'elaborate: LLM fill-in → READY\n'
        '\n'
        'PIPELINE_STATUS: READY\n'
        'ACTOR: tasksmith-elaborate\n'
        'EXECUTION_ID: {exec_id}'
    ),
    "elaborate.comment_review": (
        'elaborate: verify failed; staying REVIEW\n'
        '{listing}\n'
        '\n'
        'PIPELINE_STATUS: REVIEW\n'
        'ACTOR: tasksmith-elaborate\n'
        'EXECUTION_ID: {exec_id}'
    ),
    "events.elaborate.summary": 'Filled in design and set READY (executor: {executor})',
    "events.finish_node.done_summary": (
        'Executed with {executor_name} and completed. Outcome: {outcome} / AC done: {acs}'
    ),
    "events.finish_node.executor_suffix": ' (executor: {executor_name})',
    "events.finish_node.stopped_summary": 'Stopped execution ({state}). Reason: {reason}',
    "events.record_human_reply.summary": "Interpreted reply '{reply}' as {decision} and moved to {transitioned_to}",
    "events.request_human.summary": 'Requested human action via inbox. Reason: {reason}',
    "events.request_review.summary": 'Requested review in channel {channel}. Reason: {reason}',
    "executor.not_skill_format": 'executor is not skill:<name>: {exec_raw!r}',
    "executor.skill_missing": 'skill `{skill_name}` is missing under skills/',
    "finish_node.exception": 'finish-node exception: {error}',
    "finish_node.exception_transition_failed": 'finish-node exception (transition also failed): {error}; {inner}',
    "finish_node.marker_missing": 'Missing marker (no PIPELINE_STATUS: DONE|BLOCKED)',
    "finish_node.none": 'none',
    "finish_node.result_missing": 'Result file missing: {result_path}',
    "finish_node.result_unreadable": 'Cannot read result file: {error}',
    "human_reply.clarify_unclear": 'Could not parse the message. Reply again with a clear choice or conclusion.',
    "human_reply.clarify_unresolved": (
        'Could not lock in an interpretation. Reply in one of these forms: '
        '"A", "confirmed", "redo", "reject", or "due on YYYY-MM-DD". Reasons: {reasons}'
    ),
    "human_reply.decision_draft_line": 'Human decision: {decision_label}',
    "human_reply.engine_paused": 'Engine paused; deferring decision and retrying.',
    "human_reply.fill_incomplete": 'Still missing fields after fill-in. Reply with the missing items filled in:\n',
    "human_reply.fill_remaining": '\nfill remaining violations:\n{listing}',
    "human_reply.interpretation_audit": 'original: {message}\ninterpretation: {interpretation}',
    "human_reply.reject_note": 'Rejected',
    "human_reply.revise_note": 'Redo instructions',
    "human_reply.verify_failed_detail": (
        'verify failed\n'
        'original: {message}\n'
        'interpretation: {interpretation}\n'
        'violations: {violations}'
    ),
    "human_request.choices_prefix": 'Choices: ',
    "human_request.footer": 'Reply in this thread (e.g. "A", "confirmed", "redo").',
    "human_request.heading_acceptance": '## Acceptance criteria',
    "human_request.heading_judgments": '## Open decisions',
    "human_request.heading_objective": '## Objective / deliverables',
    "human_request.no_acceptance": '- (none)',
    "human_request.no_objective": '(not specified)',
    "human_request.recommendation": 'Recommended: {label}',
    "human_request.title": '[tasksmith request] {task_name}',
    "judgment_replace.decided": 'Decided: {choice}',
    "judgment_replace.decided_plain": 'Decided',
    "metrics.tick_elapsed": 'tick elapsed p50/p95: {p50} s / {p95} s',
    "reconcile.completable_parent": 'All {count} direct children are DONE+completed',
    "reconcile.dependency_cycle": 'Dependency cycle: [{cycle}]',
    "reconcile.dependency_pending": 'depends_on: {dep_gid} not complete',
    "reconcile.markdown.elapsed": 'Elapsed: {elapsed_seconds}s',
    "reconcile.markdown.none": '(none)',
    "reconcile.markdown.table_header": '| Task | GID | Reason | Due |',
    "reconcile.markdown.warnings_heading": '## Warnings',
    "reconcile.overdue": 'Overdue due_on={due_s}',
    "reconcile.overdue_unmanaged": 'Overdue due_on={due_s} (unmanaged)',
    "reconcile.ready": 'Dependencies satisfied; PipelineState=READY; OwnerType={owner}',
    "reconcile.waiting_external": (
        'Dependencies satisfied; waiting on external probe '
        '(PipelineState=READY; OwnerType=EXTERNAL)'
    ),
    "external.done": 'External task {name} completed',
    "external.exhausted": 'External task {name} exhausted after {count} checks',
    "external.probe_error": 'External probe error: {error}',
    "reconcile.reason_separator": ' / ',
    "reconcile.review": 'PipelineState=REVIEW; OwnerType={owner}',
    "reconcile.section_default_unsynced": 'Body section: {section_id} but default lane on store (sync not run)',
    "reconcile.unmanaged.not_fixed_and_unset": 'Not fixed format and state/owner unset',
    "reconcile.unmanaged.not_fixed_format": 'Not fixed format (is_controlled_notes=false)',
    "reconcile.unmanaged.unset": 'state/owner unset',
    "reconcile.unsectioned_no_body": 'Body has no section: line',
    "reconcile.waiting_human": 'PipelineState=WAITING_HUMAN; OwnerType=HUMAN',
    "reconcile.waiting_since": ' / waiting since={since}',
    "reconcile.warning.secretary_token_missing": 'secretary_token_missing: cannot detect drift',
    "recover.error_no_body": 'error: no body',
    "recover.error_not_blocked": 'error: state is not BLOCKED (current: {state})',
    "recover.error_not_fixed_format": 'error: not fixed format',
    "reflow.detail": 'reflow: HUMAN with empty open decisions → READY',
    "reflow.entry_gate_violation": '- {task_gid} {name!r}: {owner} → personal (gate violation; staying REVIEW)',
    "reflow.entry_ready_candidate": '- {task_gid} {name!r}: {owner} → personal (READY restore candidate)',
    "reflow.entry_ready_pending": '- {task_gid} {name!r}: {owner} → personal (READY restore planned)',
    "reflow.summary": (
        '{verb}: AI → dag {dag_count} / HUMAN → personal {personal_count} '
        '(READY restored {restored_count}), skipped {skipped_count}'
    ),
    "remind.escalated": 'Reminder: task "{name}" hit the reminder cap and moved to REVIEW.',
    "remind.option_a": 'A: handle today',
    "remind.option_b": 'B: defer to {b_date}',
    "remind.option_c": 'C: defer to {c_date}',
    "remind.option_d": 'D: withdraw',
    "remind.waiting": 'Reminder: waiting for confirmation on task "{name}".',
    "request_human.detail_with_link": '{detail}\n\nInbox: {permalink}',
    "request_human.detail_with_options": '{detail}\n\nrequest: {exec_id}\nChoices: ',
    "request_review.batch.skip_cap": 'skip review: review_daily_cap={daily_cap} reached ({already_today} today)',
    "request_review.batch.skip_remaining": 'skip review remaining: gid={task_gid} stays REVIEW (daily cap)',
    "request_review.detail_with_link": '{listing}\n\nInbox: {permalink}',
    "request_review.skip.already_requested": '_review_requested already set',
    "request_review.skip.not_review": 'state is not REVIEW (current: {state})',
    "request_review.skip.owner_invalid": 'owner is not AI/HUMAN (current: {owner})',
    "review_request.judgment_needed": 'Human decision needed: {judgment}',
    "review_request.lane_prompt": 'Pick a lane by number: ',
    "review_request.missing_heading": 'Missing / to confirm:',
    "review_request.reason_unknown": 'Could not determine REVIEW reason. Fill missing fields in the body.',
    "review_request.reply_hint": 'Reply in this thread to apply',
    "review_request.title": '[tasksmith review] {task_name}',
    "tick.noop_no_ready": 'no-op: no READY AI/HUMAN nodes',
    "tick.parent_done_detail": 'Parent completed because all direct children are DONE+completed',
    "tick.running_detail": 'tick: READY → RUNNING (generation={generation})',
    "tick.skip_ai_cap": 'skip ai: RUNNING={running} at cap --max={max_running}',
    "tick.skip_enqueue_key_in_use": 'skip enqueue: idempotency key in use gid={task_gid}',
    "tick.skip_human_cap": 'skip human: WAITING_HUMAN={waiting_count} at cap --max-human={max_human}',
    "tick.skip_human_no_channel": 'skip human: request_channel_id not set',
    "tick.skip_review_digest_quiet": 'skip review/digest: quiet_hours',
    "tick.skip_external_quiet": 'skip external probes: quiet_hours',
    "tick.unsectioned_count": 'unsectioned: {count} (section unset or still on default lane)',
}
_VIOLATIONS = {
    "create_spec.acceptance_criteria": "acceptance_criteria must be a non-empty array of strings",
    "create_spec.deliverables": "deliverables must be a non-empty array of strings",
    "create_spec.judgments": "judgments must be an array of strings (may be empty)",
    "create_spec.objective": "objective must be a non-empty string",
    "create_spec.procedure": "procedure must be a non-empty array of strings",
    "create_spec.references": "references must be an array of strings (may be empty)",
    "create_spec.schema_version": "schema_version must be 1",
    "create_spec.title": "title must be a non-empty string",
    "decompose.child.acceptance_criteria": "{prefix}.acceptance_criteria must be a non-empty array of strings",
    "decompose.child.covers_empty": "{prefix}.covers_parent_deliverables must be a non-empty 1-based array",
    "decompose.child.covers_invalid": "{prefix}.covers_parent_deliverables has invalid number: {number!r}",
    "decompose.child.deliverables": "{prefix}.deliverables must be a non-empty array of strings",
    "decompose.child.depends_on_array": "{prefix}.depends_on must be an array",
    "decompose.child.depends_on_empty": "{prefix}.depends_on has an empty element",
    "decompose.child.due_offset_days": "{prefix}.due_offset_days must be an integer 0-365",
    "decompose.child.executor_format": "{prefix}.executor must be skill:<name>",
    "decompose.child.executor_human": "{prefix}.executor must be a non-empty string for HUMAN",
    "decompose.child.judgments": "{prefix}.judgments must be an array of strings",
    "decompose.child.key": "{prefix}.key must be C1-C12",
    "decompose.child.not_object": "{prefix} must be an object",
    "decompose.child.objective": "{prefix}.objective must be a non-empty string",
    "decompose.child.owner": "{prefix}.owner must be AI or HUMAN only (EXTERNAL is phase 4)",
    "decompose.child.procedure": "{prefix}.procedure must be a non-empty array of strings",
    "decompose.child.references": "{prefix}.references must be an array of strings",
    "decompose.child.title": "{prefix}.title must be a non-empty string",
    "decompose.child_keys": "Child keys must be C1-C{count} in sequence (got {keys})",
    "decompose.children_array": "children must be an array",
    "decompose.children_count": "Child count must be 1-12 (got {count})",
    "decompose.covers_out_of_range": (
        "{key} covers_parent_deliverables={idx} exceeds parent deliverable count "
        "{parent_deliverable_count}"
    ),
    "decompose.cycle": "Dependency cycle detected",
    "decompose.due_order": (
        "{key} due_offset_days={due_offset_days} is before dependency {dep}={due_offset_days2}"
    ),
    "decompose.duplicate_dep": "Duplicate dependency {dep} for {key}",
    "decompose.parent_deliverables_empty": "Parent has no deliverables; cannot decompose",
    "decompose.schema_version": "schema_version must be 1",
    "decompose.self_dep": "{key} depends on itself",
    "decompose.uncovered_parent": "Parent deliverables not covered: {missing}",
    "decompose.unknown_dep": "Unknown dependency {dep} for {key}",
    "design.engine_error": "LLM run failed: {error}",
    "design.json_missing": "No JSON object found in output",
    "design_doc.ac_count": "Acceptance criteria count is out of range",
    "design_doc.ac_count.detail": "Acceptance criteria must be {min}-{max} items (currently {count}).",
    "design_doc.ac_vague_word": "Acceptance criteria contain vague wording",
    "design_doc.ac_vague_word.detail": "Acceptance criteria contain vague wording \"{word}\": {item}",
    "design_doc.content.executor_invalid": "AI executor must be skill:<name>",
    "design_doc.deliverable_no_location": "Deliverable has no location specified",
    "design_doc.deliverable_no_location.empty": "No deliverables listed.",
    "design_doc.deliverable_no_location.item": "Deliverable has no location: {item}",
    "design_doc.executor_not_found": "Executor skill is not set",
    "design_doc.executor_not_found.bad_format": "AI nodes must set executor as skill:<name>.",
    "design_doc.executor_not_found.missing_skill": "executor skill:{skill_name} does not exist under skills/.",
    "design_doc.external_check_unknown": "External check probe is unknown",
    "design_doc.external_check_unknown.detail": "check must name a registered external probe.",
    "design_doc.external_meta_invalid": "External check metadata is invalid",
    "design_doc.external_meta_invalid.detail": "Invalid external metadata field: {field}.",
    "design_doc.format_invalid": "Body format is invalid",
    "design_doc.format_invalid.detail": (
        "Section order does not match {current}, or the format marker is not {current}."
    ),
    "design_doc.format_outdated": "Body format version is outdated",
    "design_doc.format_outdated.detail": "Version is {version}. Upgrade to {current}.",
    "design_doc.judgment_not_empty": "Open decisions are not empty",
    "design_doc.judgment_not_empty.detail": "Open decisions are not empty; cannot move to READY.",
    "design_doc.objective_empty.detail": "Objective is empty.",
    "design_doc.section_default": "Section is set to the default lane",
    "design_doc.section_default.detail": (
        "section: is the default section ({name}). Managed tasks must use a non-default lane."
    ),
    "design_doc.section_missing": "Section lane is not set",
    "design_doc.section_missing.detail": "No section: in metadata. Specify a section lane.",
    "design_doc.section_unknown": "Section value is invalid",
    "design_doc.section_unknown.detail": "section: value {value!r} is not a known section id.",
    "elaborate.acceptance_criteria": "acceptance_criteria must be a non-empty array of strings",
    "elaborate.deliverables": "deliverables must be a non-empty array of strings",
    "elaborate.executor_missing": "executor must be a non-empty string",
    "elaborate.procedure": "procedure must be a non-empty array of strings",
    "human_reply.cascade_bool": "cascade must be a bool true/false",
    "human_reply.choice_not_in_options": "choice {choice!r} is not in options {choices}",
    "human_reply.choice_not_string": "choice must be a string",
    "human_reply.decision_invalid": (
        "decision must be approve / done / revise / reject / defer / unclear / fill / replan"
    ),
    "human_reply.due_on_format": "due_on must be YYYY-MM-DD",
    "human_reply.due_on_future": "due_on must be a future date",
    "human_reply.json_missing": "No JSON object in output (emit exactly one ```json fence)",
    "human_reply.note_not_string": "note must be a string",
    "human_reply.split_array": "split must be an array of strings",
}

_WORD_LISTS = {
    "ac_vague": ('works correctly', 'appropriately', 'without problems', 'properly', 'as needed'),
    "location_keywords": ('comment', 'attachment'),
    "colons": (":",),
    "choice_label_lead": (" ", "/", "(", "("),
    "recommend": ("recommend",),
}

_PROMPTS = {
    "human_reply.none": "(none)",
    "human_reply.prompt": (
        "# tasksmith record_human_reply\n\n"
        "Interpret the human reply and emit exactly one ```json fence.\n\n"
        "```json\n"
        '{{"decision": "approve|done|reject|revise|defer|unclear|fill|replan", '
        '"choice": "A", "note": "...", "due_on": "YYYY-MM-DD", "cascade": true, '
        '"split": ["subtask"], "executor": "skill:<name>", "deliverables": ["..."], '
        '"acceptance": ["..."], "judgment_choice": "A"}}\n'
        "```\n\n"
        "Choices: {choice_hint}\n"
        "Objective:\n{objective}\n\n"
        "Open decisions:\n{judgment}\n\n"
        "Working draft:\n{draft}\n\n"
        "Acceptance criteria:\n{acceptance}\n\n"
        "Human message:\n{message}\n"
    ),
    "run_prompt.contract_ac_done": "- AC done: `AC_DONE: 1,3` (1-indexed, comma-separated)\n",
    "run_prompt.contract_blocked": "- Failure / blocked: `PIPELINE_STATUS: BLOCKED`\n",
    "run_prompt.contract_done": "- Success: `PIPELINE_STATUS: DONE`\n",
    "run_prompt.contract_intro": "End with these markers on their own lines.\n",
    "run_prompt.contract_note": "- Draft note: `NOTE: <text>` (multiple allowed)\n",
    "run_prompt.forbidden_fields": (
        "Do not change: format / state / owner / executor / depends_on / inputs / outputs, "
        "index tags, or section structure.\n"
    ),
    "run_prompt.forbidden_outputs": (
        "Put outcomes in external artifacts and comments; only `NOTE:` / `AC_DONE:` in the body.\n"
    ),
    "run_prompt.heading_acceptance": "## Acceptance criteria\n",
    "run_prompt.heading_contract": "## Output contract\n",
    "run_prompt.heading_deliverables": "## Deliverables\n",
    "run_prompt.heading_draft": "## Working draft (current)\n",
    "run_prompt.heading_forbidden": "## Forbidden (protect control metadata)\n",
    "run_prompt.heading_objective": "## Objective\n",
    "run_prompt.heading_procedure": "## Procedure and constraints\n",
}

EN = LanguagePack(
    headings=_HEADINGS,
    original_notes_heading="Original body before normalization",
    meta_tip="(AI-managed fields; do not edit by hand.)",
    messages=_MESSAGES,
    violations=_VIOLATIONS,
    word_lists=_WORD_LISTS,
    prompts=_PROMPTS,
)
