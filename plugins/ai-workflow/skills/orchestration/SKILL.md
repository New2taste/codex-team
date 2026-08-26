---
name: orchestration
description: Use when running the Codex Team companion-agent preflight or bounded distribution checks that require verified Luna discovery, explicit execution-surface distinctions, and evidence gates.
---

# Codex Team preflight and distribution contract

Optimization routing stays in `shadow` by default and is read from
`[optimization]`, never from `route --mode`. `actual_route` and
`recommended_route` are runtime advice plus an `ai-route-advice-1`
sidecar, and they never change the frozen `ai-route-decision-1` wire or
the effective roles. The public facade computes the gate internally;
callers cannot supply `ALLOW_ENFORCED`. `enforced` may apply a closed-set
cost-downgrade only when the cost claim is `COST_REDUCTION_SUPPORTED` at
eight paired cases, P0/P1 misses are zero, and experiment first-delivery
is not below calibration; otherwise the fixed chain is used. Missing
miss reports and defaulted period/origin cannot open the gate. Shadow
schedulers do not execute recommendations.

Compact prompts are a dual-key armed field projection, not a summary or
LLM compression. Public builders decide only from the pinned
`[optimization]` config and `aggregate_metrics(state_root)`; callers
cannot pass config or metrics to arm compact. They apply only when
`[optimization].compact_prompts` is true, `optimization.mode` is
`enforced`, `evaluate_optimization_gate(metrics)` is `ALLOW_ENFORCED`,
and the compact UTF-8 payload is smaller than full. Shadow mode, a
missing state_root, missing or illegal metrics, and a closed gate always
keep the full prompt. The projection must keep task_id, schema/role
identity and role instructions, objective, repository_root/source_worktree,
commits, authoritative files, write paths, forbidden actions, risk flags,
acceptance commands, verification level, human gates, the two evidence
authorization sentences, and any present plan/step id, write_scope,
acceptance criteria, dependencies, permission profile, hashes,
runtime/session bindings, owner decisions or authorization tickets, and
required output schema/path verbatim. Unknown critical fields are
retained or compact is disabled. Acceptance repair ladder assignment
prompts do not participate in compact and stay full.

The default Luna route uses a controller-dispatched native subagent. The
controller must record
`execution_surface=NATIVE_SUBAGENT`, `model=gpt-5.6-luna`,
`reasoning_effort=max`, native thread/agent evidence, sandbox, permission, and
cwd before promoting a result. A native `luna` dispatch and an automated
`codex exec -m gpt-5.6-luna` role-contract invocation are different execution
surfaces. Neither surface may erase the task envelope, L0/L1/L2 evidence
level, human owner gates, or the final-acceptance boundary.

## Frozen role and lifecycle contract

- **Luna Max** may write only inside an exact frozen envelope. Eligible work is
  medium/low-complexity mechanical coding, deterministic verification, and
  root-to-Plugin distribution. The envelope must name every path, command,
  negative check, and artifact. Luna must never review, approve, or perform
  final acceptance.
- **Intermediate engineering sections** are `section_self_check_only`: their
  construction owner must run the frozen-envelope checks, target tests,
  negative checks, scope checks, and runtime-evidence gate, then advance to
  the next section. Do not dispatch a separate adversarial review per section.
- **Terra xhigh** owns complex construction, integration, and open-ended
  debugging in an isolated worktree. Its construction self-check is not an
  independent acceptance or a permission to self-approve.
- **Sol medium** performs one read-only, adversarial **Sol-medium final
  acceptance** after every engineering section is complete. The scheduler
  APIs are `create_final_acceptance_case` (unique `ACCEPTANCE` child after
  every receipt; final candidate may descend from the frozen plan candidate
  if HEAD/scope/child-hash bind) and `issue_final_acceptance` (one
  Sol-medium `REVIEW_1` only, retryable after a failed assignment append).
  If it returns
  `REWORK`, a **different Sol-medium fixer** may receive one
  owner-authorized, assignment-scoped write capability limited to the frozen
  findings, candidate, paths, and verification commands. The accepting
  reviewer never repairs its own verdict. A **different Sol-medium recheck**
  (different from both acceptor and fixer) is read-only and may not widen
  scope. Owner xhigh authorization is `decide <task_id> authorize_final_xhigh`
  and must not be combined with `--resume`.
- **Sol xhigh** handles owner-authorized planning and terminal escalation in a
  closed case file. Only a `REWORK` from that different Sol-medium recheck may
  authorize one terminal repair without task-level review. It never starts
  automatically and never bypasses final Sol-medium acceptance.
- Only roles explicitly listed in the frozen configuration may be selected;
  unspecified models and reasoning levels are never silently substituted.

## Scheduler CLI

The production zero-model controller path is `schedule-batch` →
`schedule-result` → `schedule-receipt` → `schedule-final`. The batch command
replays a validated FrozenPlan. After the existing execution boundary produces
an `ai-result-1`, `schedule-result` derives the output location from the bound
dispatch and atomically freezes
`<state_root>/<task_id>/scheduler-results/<dispatch_id>.json`; callers cannot
choose that destination. The controller adds and verifies the
`dispatch_id`/`task_id`/`step_id`/`attempt` self-binding and rejects symlinks,
hardlinks, directory replacement, and oversized results. It emits the
hash-bound receipt consumed by `schedule-receipt`.

After all receipts complete, `schedule-final` creates the unique acceptance
child and its directed `scheduler-parent.json` binding. Supplying both a
verified `--owner-receipt` and a Sol-medium `--acceptor` issues the single
`REVIEW_1`. If the bounded repair ladder later reaches terminal escalation,
the owner uses `decide <child_id> authorize_final_xhigh`; no scheduler command
starts a model, merges, or pushes.

## Codex Team natural-language contract

Accept only a leading, case-insensitive directive in one of these approved
forms. `<objective>` whitespace is normalized; it is never interpreted as a
shell command:

```text
team call <objective>
team call: <objective>
team call：<objective>
```

The default is **single active worker**. A non-terminal receipt blocks another
call, so this contract does not promise parallel agents or parallel worktree
writes.

| Disposition | Allowed route | Boundary |
|---|---|---|
| `DIRECT_L0` | Exact fixed L0 allowlist only. | **L0 controller/no model**: the controller runs the registered argv and starts no model. |
| `DIRECT_L1` | Exact `核对文件 <repo-relative-path>` evidence request only. | **L1 Luna read-only**: Luna may extract the pinned evidence only; the existing L0/L1/L2 evidence contract still applies. |
| `PLAN_REQUIRED` | Any other safe objective. | **plan fallback** to the existing frozen-envelope workflow with human owner gates; do not automatically invoke Sol xhigh. |
| `BLOCKED` | Invalid input, an active receipt, missing authority, or failed execution. | Write a blocking receipt; do not promise a model run, task, merge, or push. |

Team Call neither grants Luna review, approval, or final-acceptance authority
nor weakens the existing L0/L1/owner-gate contract. It does not auto-merge or
auto-push, and it never replaces human approval or final whole-project
acceptance.

For the CLI, an omitted `--root` selects per-repository state below
`$XDG_STATE_HOME/ai-workflow/team-call/` (or the user state directory), outside
the repository under review; an explicit `--root` remains authoritative. A
terminal failed call replays as a `BLOCKED` receipt and exits 2. An explicitly
authorized `--runner live --allow-live-model` DIRECT_L1 uses Luna Max through a
write-once evidence snapshot bound to the exact stored-task digest, `luna`
role, execution surface, and consumed-evidence digest. It still requires a
verifiable `--runtime-sessions-dir` and the repository/Git-control/zero-write
guards. This binding does not grant review, approval, construction, or final
acceptance authority.

## Luna distribution envelope

For a Luna-owned distribution task, the writable envelope is limited to the
following files and exact source-to-Plugin copy pairs. Do not modify routing,
repair semantics, automatic merge/push behavior, or any file outside this
list.

### Runtime copies

```text
scripts/ai_workflow.py           -> plugins/ai-workflow/runtime/ai_workflow.py
scripts/ai_workflow_artifacts.py -> plugins/ai-workflow/runtime/ai_workflow_artifacts.py
scripts/ai_workflow_costs.py     -> plugins/ai-workflow/runtime/ai_workflow_costs.py
scripts/ai_workflow_planning.py  -> plugins/ai-workflow/runtime/ai_workflow_planning.py
scripts/ai_workflow_repairs.py   -> plugins/ai-workflow/runtime/ai_workflow_repairs.py
scripts/ai_workflow_routing.py   -> plugins/ai-workflow/runtime/ai_workflow_routing.py
scripts/ai_workflow_runtime.py   -> plugins/ai-workflow/runtime/ai_workflow_runtime.py
scripts/ai_workflow_team_call.py -> plugins/ai-workflow/runtime/ai_workflow_team_call.py
scripts/ai_workflow_scheduler.py -> plugins/ai-workflow/runtime/ai_workflow_scheduler.py
```

### Schema and configuration copies

```text
config/ai_workflow.toml                    -> plugins/ai-workflow/config/ai_workflow.toml
config/ai_workflow_task.schema.json        -> plugins/ai-workflow/config/ai_workflow_task.schema.json
config/ai_workflow_result.schema.json      -> plugins/ai-workflow/config/ai_workflow_result.schema.json
config/ai_workflow_route_request.schema.json -> plugins/ai-workflow/config/ai_workflow_route_request.schema.json
config/ai_workflow_route_decision.schema.json -> plugins/ai-workflow/config/ai_workflow_route_decision.schema.json
config/ai_workflow_route_advice.schema.json -> plugins/ai-workflow/config/ai_workflow_route_advice.schema.json
config/ai_workflow_plan.schema.json        -> plugins/ai-workflow/config/ai_workflow_plan.schema.json
config/ai_workflow_runtime_evidence.schema.json -> plugins/ai-workflow/config/ai_workflow_runtime_evidence.schema.json
config/ai_workflow_cost_evidence.schema.json -> plugins/ai-workflow/config/ai_workflow_cost_evidence.schema.json
config/ai_workflow_scheduler.schema.json    -> plugins/ai-workflow/config/ai_workflow_scheduler.schema.json
```

The permitted documentation and evidence files are `README.md`, this
`SKILL.md`, `plugins/ai-workflow/skills/orchestration/agents/openai.yaml`,
`plugins/ai-workflow/scripts/verify.sh`, and
`tests/test_ai_workflow_distribution.py`.

## Required checks

Use the fixed root manifest synchronizer after root runtime/config changes, then
run the single zero-model verification entrypoint:

```sh
python3.11 scripts/sync_plugin.py --check
sh scripts/verify_all.sh
```

Use `scripts/sync_plugin.py --write` only when the root authority is intended to
replace the Plugin copies. The verifier must compare every listed
source-to-Plugin pair byte-for-byte.
For the negative check, copy the release into a temporary target, tamper one
mirrored runtime or schema file, and confirm `verify.sh` exits non-zero; never
leave the tamper in the repository. Also run the published role/lifecycle
language assertions, full unittest discovery, Python compileall, shell syntax
checks, and `git diff --check` before reporting.
