---
name: orchestration
description: Run the AI Workflow companion-agent preflight for bounded Codex tasks that need verified Luna discovery, explicit execution-surface distinctions, and evidence gates.
---

# AI Workflow Preflight

Run the installed plugin's companion preflight before relying on the custom
Agent. Resolve the installed plugin directory first, then run:

```sh
sh "$plugin_dir/scripts/install-agents.sh" --check
```

Stop if preflight fails. Require the exact custom Agent name `luna_worker`; do
not substitute `worker` or any built-in Agent.

Treat a native interactive `luna_worker` invocation and an automated
`codex exec -m gpt-5.6-luna` role-contract invocation as different execution
surfaces. The latter is not evidence of a native custom Agent. Preserve the
task envelope, L0/L1/L2 evidence level, human owner gates, and final-acceptance
boundary in either case.

This initial release ends after preflight. It does not start a model, route a
task, grant approvals, or change execution ownership. The execution OS remains
Terra-led; Sol is an expert co-processor and escalation/review path, while
Luna is only a low-cost bounded tool process when explicitly assigned.
