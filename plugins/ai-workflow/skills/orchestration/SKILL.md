---
name: orchestration
description: Use when running the AI Workflow companion-agent preflight or bounded distribution checks that require verified Luna discovery, explicit execution-surface distinctions, and evidence gates.
---

# AI Workflow preflight and distribution contract

Run the installed plugin's companion preflight before relying on the custom
Agent. Resolve the installed plugin directory first, then run:

```sh
sh "$plugin_dir/scripts/install-agents.sh" --check
```

Stop if preflight fails. Require the exact custom Agent name `luna_max`; do
not substitute `worker` or any built-in Agent. A native interactive
`luna_max` invocation and an automated
`codex exec -m gpt-5.6-luna` role-contract invocation are different execution
surfaces. Neither surface may erase the task envelope, L0/L1/L2 evidence
level, human owner gates, or the final-acceptance boundary.

## Legacy installer migration (one-time)

If an existing install contains `luna_worker` as one-time migration input, the
installer may process it only after validating the legacy template,
state, and backup. The old spelling is never a current selectable or invoked
Agent; the workflow role remains `luna`.

## Frozen role and lifecycle contract

- **Luna Max** may write only inside an exact frozen envelope. Eligible work is
  medium/low-complexity mechanical coding, deterministic verification, and
  root-to-Plugin distribution. The envelope must name every path, command,
  negative check, and artifact. Luna must never review, approve, or perform
  final acceptance.
- **Terra xhigh** owns complex construction, integration, and open-ended
  debugging in an isolated worktree. Every task needs an independent Terra
  xhigh adversarial review in a distinct context; the constructor never
  reviews itself.
- **Sol medium** performs final whole-project acceptance. It may perform one
  scoped fallback only after the second Terra xhigh failure, followed by a
  distinct Sol-medium peer review. It is not an ordinary planning or
  construction role.
- **Sol xhigh** handles owner-authorized planning and terminal escalation in a
  closed case file. It never starts automatically and never bypasses final
  Sol-medium acceptance.
- **Terra medium** and **Sol high** have no default role. They must not be
  silently introduced as substitutes.

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
```

### Schema and configuration copies

```text
config/ai_workflow.toml                    -> plugins/ai-workflow/config/ai_workflow.toml
config/ai_workflow_task.schema.json        -> plugins/ai-workflow/config/ai_workflow_task.schema.json
config/ai_workflow_result.schema.json      -> plugins/ai-workflow/config/ai_workflow_result.schema.json
config/ai_workflow_route_request.schema.json -> plugins/ai-workflow/config/ai_workflow_route_request.schema.json
config/ai_workflow_route_decision.schema.json -> plugins/ai-workflow/config/ai_workflow_route_decision.schema.json
config/ai_workflow_plan.schema.json        -> plugins/ai-workflow/config/ai_workflow_plan.schema.json
config/ai_workflow_runtime_evidence.schema.json -> plugins/ai-workflow/config/ai_workflow_runtime_evidence.schema.json
config/ai_workflow_cost_evidence.schema.json -> plugins/ai-workflow/config/ai_workflow_cost_evidence.schema.json
```

The permitted documentation and evidence files are `README.md`, this
`SKILL.md`, `plugins/ai-workflow/skills/orchestration/agents/openai.yaml`,
`plugins/ai-workflow/scripts/verify.sh`, and
`tests/test_ai_workflow_distribution.py`. A report belongs at
`.superpowers/sdd/2026-08-08-adversarial-acceptance-luna-allocation/task-4-report.md`;
append a short entry to that directory's `progress.md`.

## Required checks

Run the Plugin verifier and the focused distribution suite after every copy:

```sh
sh plugins/ai-workflow/scripts/verify.sh
/Users/lee/.local/bin/python3.11 -m unittest tests.test_ai_workflow_distribution
```

The verifier must compare every listed source-to-Plugin pair byte-for-byte.
For the negative check, copy the release into a temporary target, tamper one
mirrored runtime or schema file, and confirm `verify.sh` exits non-zero; never
leave the tamper in the repository. Also run the published role/lifecycle
language assertions, full unittest discovery, Python compileall, shell syntax
checks, and `git diff --check` before reporting.
