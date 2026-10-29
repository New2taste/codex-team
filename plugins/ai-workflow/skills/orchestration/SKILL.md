---
name: orchestration
description: Use when routing or executing Codex Team, team call, or multi-model collaboration tasks with bounded scope, runtime evidence, and owner gates.
---

# Codex Team

Turn an authorized objective into verified work, not just a routing label. The `balanced_terra61` policy selects an owner directly by risk and complexity; it is not a sequential model chain. Use `gpt-6.1-terra / medium` for evidenced routine automation, information work, simple planning and clearly local routine development. Sol medium retains engineering decisions, complex/cross-module implementation, state machines, permissions, concurrency, migrations and uncertain classification. Do not automatically send missing plans to Astra.

## Start and continue

Accept a leading case-insensitive `team call <objective>`, `team call: <objective>` or `team call：<objective>`. Objectives are task text, never shell commands. The CLI routes exact fixed checks to `DIRECT_L0` (no model), exact `核对文件 <repo-relative-path>` to `DIRECT_L1` (read-only Luna), and other safe objectives to `PLAN_REQUIRED`.

For general work, read [task-continuation.md](references/task-continuation.md), including its structured routine entry. The CLI creates a durable task and hash-bound planning handoff. Its fake/default mode records `PENDING_NATIVE_DISPATCH`, not a model run. Unclassified natural-language intake conservatively uses Sol medium planning. Consume the handoff's pinned role/model/effort; explicit live planning stops at the existing owner gate and does not implement. Dispatch the selected actual role using available native agent tools. A label, task ID, process launch or handoff is not completion.

For an already-authorized implementation request, continue routine planning, construction and relevant verification without asking again at every stage. Keep actual owner-controlled decisions; do not invent approvals or erase required execution authorization. Never merge or push automatically. If the execution boundary or authority is unavailable, report the exact unfinished action and retained evidence.

## Roles and limits

- **Luna Max** (`gpt-6-luna`, `reasoning_effort=max`) owns only an exact frozen envelope for mechanically verifiable bounded work. Batch related work; return failures to the primary implementer. Never plan, review, approve or self-accept. Default native execution requires `execution_surface=NATIVE_SUBAGENT` and observed model/effort, thread/agent, cwd, sandbox and permission evidence. CLI role-contract execution is a distinct surface.
- **Terra 6.1 medium** uses new `terra61_planner` (read-only) and `terra61_worker` (scoped writes) roles. Select only after source inspection confirms routine local work; freeze `ROUTINE_LOCAL_CONFIRMED` and `SOURCE_INSPECTION_REQUIRED` in the route request. No keywords, model self-confidence or missing risk flags alone establish eligibility. Contradictory/dependent/high-consequence classification retains Sol.
- **Sol medium** owns engineering decisions, complex implementation, uncertain planning, integration, debugging and first final-acceptance rework. Keep all scope, task, evidence, permission and identity fields. Historical `terra`, `terra_xhigh` and related IDs remain Sol; never remap existing tasks to Terra. A missing old model or incompatible binding blocks explicitly, without substitution.
- Intermediate engineering sections use `section_self_check_only`; do not dispatch adversarial review for each section.
- **GPT-6.1 Sol high final acceptance** is one independent, read-only, adversarial final acceptance after all sections complete. Review actual behavior, diff, edge cases and failures; collect actionable defects together.
- A `REWORK` permits a different GPT-6.1 Sol medium fixer with owner-authorized assignment-scoped writes, then an independent read-only Sol medium recheck; Astra medium is optional. A second `REWORK` permits one existing owner-authorized Sol high or Astra medium terminal repair without task-level review. Existing functional and release gates still apply.
- Astra medium handles explicitly authorized large-project planning/disputes. It never starts automatically and never bypasses authority or final acceptance.
- At most two subagents, one writer and two readers. Team Call retains one active receipt. Do not add agents for trivial or sequential work.

## Evidence and environment

Preserve L0/L1/L2 evidence and human gates. Do not infer a model run from classification or a fabricated receipt. Prompt prefixes keep stable instructions, output identity and permission constraints before dynamic task/evidence; compact prompts stay gated and retain every critical field.

Permission denials, missing credentials, quota exhaustion and macOS Chrome `RegisterApplication`/LaunchServices startup failures are execution-environment blockers, not implementation rework. Do not repeat the same blocked operation, upgrade models as a remedy, disable sandbox globally or bypass approval. Use an appropriate already-authorized execution path, obtain the platform-required permission, or report a concrete blocker. Ordinary application assertion crashes remain defects.

Read [advanced-contracts.md](references/advanced-contracts.md) only when using scheduler, frozen construction/repair, optimization gates, runtime preflight or distribution sync. It includes existing schema and exact copy contracts. After root runtime/config edits, use `sync_plugin.py --write` when those files are authoritative; verify with `PYTHON_BIN=python3.11 sh scripts/verify_all.sh`.

For a real single-Sol-medium comparison, read [measurement.md](references/measurement.md). Reuse existing cost/report evidence; do not claim savings before matched real tasks. Luna lower-effort trials remain bounded experiments, not production defaults.

Terra max is only an explicitly selected experimental candidate for complex visual/computer-use work, not a production role or automatic escalation. Community ratings are small non-engineering samples, not evidence to upgrade all roles; retain Luna max and Astra's bounded planning/dispute responsibility.
