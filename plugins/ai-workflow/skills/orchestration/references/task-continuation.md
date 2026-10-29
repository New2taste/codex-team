# General Team Call continuation

## Durable intake

Use the Plugin runtime `ai_workflow.py team-call MESSAGE --repository-root ABSOLUTE_REPO`. Default/fake mode is zero-model intake, not planning or execution. It returns a task ID; inspect `<state_root>/<task_id>/team-call-handoff.json` and `task.json`. The default state root is outside the repository; use an explicit absolute `--root` when continuity matters. Repeating the exact call replays its receipt without a second task or launch. Re-run intake before consumption to validate receipt, task, repository, root and handoff digest. Do not change or reuse a handoff for another objective/repository.

`--runner live --allow-live-model --runtime-sessions-dir ABSOLUTE_DIR` uses actual Sol medium read-only planning through the existing route, dispatch permit, cost accounting, runtime identity and state machine. It requires configured runtime evidence/preflight and a clean read-only repository. It stops at `AWAITING_OWNER_DECISION`; inspect the stored planner result. Missing authority or runtime evidence fails closed. Failed calls remain terminal `BLOCKED`; inspect the preserved task/handoff and evidence, do not replay to silently relaunch.

## Native continuation

If `planning_status=PENDING_NATIVE_DISPATCH`, consume the pinned role/model/effort in the handoff (unclassified natural-language intake conservatively selects Sol medium). For pre-0.5 handoffs without a role, retain the stored Sol model/effort; never infer Terra from an old role name. Dispatch with the user's objective, repository context, authoritative task and handoff. Ask for the smallest bounded plan, exact write paths, forbidden actions, relevant acceptance commands and evidence. Use fresh context with stable instructions before dynamic task data. Do not send only a routing label. Record observed agent/thread ID, model, effort, cwd and actual permissions from tools/runtime; requested identity alone is not evidence. A native planner needs actual read-only permissions, not merely a read-only prompt. Preserve output and real status in a separate continuation record alongside the handoff, never rewrite the immutable intake/result binding or label a pending task complete.

If CLI planning already ran, consume its actual result and owner decisions instead of dispatching a second planner. For implementation, create the existing remediation task and `ai-plan-1` frozen construction/request bindings from the actual plan. Exact scopes, done conditions, verification commands, permissions and owner decisions must survive the transfer. Use the existing owner-authorized construction/worktree path and scheduler receipts described in [advanced-contracts.md](advanced-contracts.md), or an available native controller with equivalent evidence/authority bindings; native dispatch is not permission to bypass CLI gates. A PLAN task with no write paths cannot become a writer by changing a field in place.

The user may already authorize routine implementation in their request. Do not ask again merely because work moved to another agent or stage; record that authorization through the actual supported owner boundary where required. Ask only for missing scope, new authority or platform approval. If a required controller capability is unavailable, report that limitation rather than manufacture an owner receipt, runtime evidence or success result.

Run the real functional checks and inspect actual behavior and changes. Sections use self-check only. After all work completes, dispatch independent Sol high read-only final acceptance within the two-agent/one-writer bound. Return the verified result and remaining limitations. Do not claim the CLI alone performs end-to-end construction, merging or publication.

## Structured routine Terra 6.1 entry

Source inspection must establish that the task is routine local work, not engineering decisions, cross-module work, state machines, permissions, concurrency or migrations. Record that assessment and source/diff evidence alongside the task; `ROUTINE_LOCAL_CONFIRMED` is an explicit classifier assertion, not proof by itself or execution authority. No keyword classifier lowers risk. Unknown classification stays Sol.

Use a normal `ai-task-1` and an `ai-route-request-1` with the same task ID:

```json
{
  "schema_version": "ai-route-request-1",
  "task_id": "AWF-20261029-001",
  "work_class": "PLANNING_ONLY",
  "execution_need": "READ_ONLY",
  "decomposable": true,
  "risk_flags": [],
  "reason_codes": ["ROUTINE_LOCAL_CONFIRMED", "SOURCE_INSPECTION_REQUIRED"]
}
```

A `PLAN` task plus this request selects only `terra61_planner` (`gpt-6.1-terra / medium`, read-only). Default requests omit the positive classification and retain Sol. Freeze using `new --task TASK --root STATE`, then `route --task TASK --request REQUEST --root STATE`. `run TASK --role terra61_planner --runner live --allow-live-model --runtime-sessions-dir SESSIONS --root STATE` uses the configured read-only planning runner and still stops at the owner gate. The explicit planner role must match the frozen route; the old default live Luna evidence entry is unchanged. Runtime sessions and preflight must be real; no fabricated receipts.

For actual local construction, create a separate `REMEDIATION` task, change the request to `BOUNDED` / `WRITE`, and provide an `ai-plan-1` with one step: `owner_role=terra61_worker`, no dependencies, one exact existing file in `write_scope`, that file also in `read_scope` and parent `authoritative_files`, frozen done conditions and verification commands. For example, fix only `labels.py` while `README.md` and `tests/test_labels.py` remain read-only. The current narrow routine gate deliberately does not cover new-file or multi-file construction; use Sol for those. Do not attach Luna's reserved `construction_envelope` field to a Terra step. Missing or invalid plan evidence cannot select a Terra writer.

```sh
python3.11 scripts/ai_workflow.py new --task TASK --root STATE
python3.11 scripts/ai_workflow.py route --task TASK --request REQUEST --construction-plan PLAN --construction-step STEP --root STATE
python3.11 scripts/ai_workflow.py run TASK --construction-plan PLAN --construction-request REQUEST --construction-step STEP --attempt 1 --runner fake --root STATE
python3.11 scripts/ai_workflow.py decide AWF-20261029-001 approve_execution --by OWNER --root STATE
python3.11 scripts/ai_workflow.py resume AWF-20261029-001 --runner live --allow-live-model --runtime-sessions-dir SESSIONS --root STATE
```

`TASK`, `REQUEST`, `PLAN`, `STATE` and `SESSIONS` are absolute file/directory paths; `STEP` and `OWNER` are the actual frozen step ID and authorized owner identity. The initial fake run only freezes construction authority and reaches the owner gate; it starts no model and is not evidence of construction. `decide` records the existing owner decision bound to construction authority in `STATE/<task_id>/human-decisions.jsonl`; live `resume` creates the task-scoped worktree and dispatches the selected actual model. Parent write paths and commands, plan hashes, dispatch permits, ownership and runtime identity are unchanged safety requirements. Run functional checks in the worktree and inspect the actual diff; `IMPLEMENTED_CANDIDATE` is not final acceptance.

Rules `BALANCED_TERRA61_ROUTINE_PLAN` / `BALANCED_TERRA61_ROUTINE_WRITE` pin new Terra identities. Old rule IDs restore the old Sol roles, even when the installation default changes. Never edit a stored task/plan/request to switch owners; use a new authorized task when scope or classification changes. Model unavailability, credentials, permissions or quota failure blocks without model substitution or repeated launches.
