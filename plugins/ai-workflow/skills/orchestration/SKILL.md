---
name: orchestration
description: Route and execute Codex Team, team call, and multi-model collaboration tasks through Sol planning/construction, bounded Luna work, runtime evidence, and owner gates.
---

# Codex Team

Turn an authorized objective into verified work, not just a routing label. General tasks use the primary implementer `gpt-6.1-sol / medium` for ordinary planning, complex implementation, integration and debugging. Do not automatically send missing plans to Astra.

## Start and continue

Accept a leading case-insensitive `team call <objective>`, `team call: <objective>` or `team call：<objective>`. Objectives are task text, never shell commands. The CLI routes exact fixed checks to `DIRECT_L0` (no model), exact `核对文件 <repo-relative-path>` to `DIRECT_L1` (read-only Luna), and other safe objectives to `PLAN_REQUIRED`.

For general work, read [task-continuation.md](references/task-continuation.md). The CLI creates a durable task and hash-bound planning handoff. Its fake/default mode records `PENDING_NATIVE_DISPATCH`, not a model run. Explicit live mode invokes Sol medium planning with verified runtime identity and stops at the existing owner gate; it does not implement. Consume the handoff in this skill and dispatch actual Sol medium work using available native agent tools. A label, task ID, process launch or handoff is not completion.

For an already-authorized implementation request, continue routine planning, construction and relevant verification without asking again at every stage. Keep actual owner-controlled decisions; do not invent approvals or erase required execution authorization. Never merge or push automatically. If the execution boundary or authority is unavailable, report the exact unfinished action and retained evidence.

## Roles and limits

- **Luna Max** (`gpt-6-luna`, `reasoning_effort=max`) owns only an exact frozen envelope for mechanically verifiable bounded work. Batch related work; return failures to the primary implementer. Never plan, review, approve or self-accept. Default native execution requires `execution_surface=NATIVE_SUBAGENT` and observed model/effort, thread/agent, cwd, sandbox and permission evidence. CLI role-contract execution is a distinct surface.
- **Sol medium** owns ordinary planning and implementation. Keep all scope, task, evidence, permission and identity fields. Historical role IDs are compatibility handles, not proof of model identity.
- Intermediate engineering sections use `section_self_check_only`; do not dispatch adversarial review for each section.
- **GPT-6.1 Sol high final acceptance** is one independent, read-only, adversarial final acceptance after all sections complete. Review actual behavior, diff, edge cases and failures; collect actionable defects together.
- A `REWORK` permits a different GPT-6.1 Sol medium fixer with owner-authorized assignment-scoped writes, then an independent read-only Sol medium recheck; Astra medium is optional. A second `REWORK` permits one existing owner-authorized Sol high or Astra medium terminal repair without task-level review. Existing functional and release gates still apply.
- Astra medium handles explicitly authorized large-project planning/disputes. It never starts automatically and never bypasses authority or final acceptance.
- At most two subagents, one writer and two readers. Team Call retains one active receipt. Do not add agents for trivial or sequential work.

## Evidence and environment

Preserve L0/L1/L2 evidence and human gates. Do not infer a model run from classification or a fabricated receipt. Prompt prefixes keep stable instructions, output identity and permission constraints before dynamic task/evidence; compact prompts stay gated and retain every critical field.

Permission denials and macOS Chrome `RegisterApplication`/LaunchServices startup failures are execution-environment blockers, not implementation rework. Do not repeat the same sandboxed Chrome startup, upgrade models as a remedy, disable sandbox globally or bypass approval. Use an appropriate already-authorized execution path, obtain the platform-required permission, or report a concrete blocker. Ordinary application assertion crashes remain defects.

Read [advanced-contracts.md](references/advanced-contracts.md) only when using scheduler, frozen construction/repair, optimization gates, runtime preflight or distribution sync. It includes existing schema and exact copy contracts. After root runtime/config edits, use `sync_plugin.py --write` when those files are authoritative; verify with `PYTHON_BIN=python3.11 sh scripts/verify_all.sh`.

For a real single-Sol-medium comparison, read [measurement.md](references/measurement.md). Reuse existing cost/report evidence; do not claim savings before matched real tasks. Luna lower-effort trials remain bounded experiments, not production defaults.
