#!/bin/sh
# Verify that the release bundle has no stale manually maintained contract copies.
set -eu

fail() {
    printf '%s\n' "ai-workflow plugin verification failed: $1" >&2
    exit 1
}

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P) || fail "plugin path"
plugin_root=$(CDPATH= cd -- "$script_dir/.." && pwd -P) || fail "plugin root"
repository_root=$(CDPATH= cd -- "$plugin_root/../.." && pwd -P) || fail "repository root"

[ -f "$plugin_root/.codex-plugin/plugin.json" ] || fail "missing manifest"
[ -f "$plugin_root/skills/orchestration/SKILL.md" ] || fail "missing orchestration skill"
[ -f "$plugin_root/skills/orchestration/agents/openai.yaml" ] || fail "missing skill metadata"
[ ! -e "$repository_root/.codex/agents/luna-max.toml" ] && [ ! -L "$repository_root/.codex/agents/luna-max.toml" ] || fail "custom Agent template present"
[ ! -e "$plugin_root/agents/luna-max.toml" ] && [ ! -L "$plugin_root/agents/luna-max.toml" ] || fail "custom Agent mirror present"
[ ! -e "$plugin_root/agents/luna-worker.toml" ] && [ ! -L "$plugin_root/agents/luna-worker.toml" ] || fail "legacy Agent template present"
[ ! -e "$repository_root/.codex/agents/luna-worker.toml" ] && [ ! -L "$repository_root/.codex/agents/luna-worker.toml" ] || fail "legacy Agent mirror present"

for name in \
    ai_workflow.toml \
    ai_workflow_task.schema.json \
    ai_workflow_result.schema.json \
    ai_workflow_route_request.schema.json \
    ai_workflow_route_decision.schema.json \
    ai_workflow_plan.schema.json \
    ai_workflow_runtime_evidence.schema.json \
    ai_workflow_cost_evidence.schema.json
do
    cmp -s "$repository_root/config/$name" "$plugin_root/config/$name" || fail "config copy differs"
done

for name in \
    ai_workflow.py \
    ai_workflow_artifacts.py \
    ai_workflow_routing.py \
    ai_workflow_planning.py \
    ai_workflow_runtime.py \
    ai_workflow_costs.py \
    ai_workflow_repairs.py \
    ai_workflow_team_call.py
do
    cmp -s "$repository_root/scripts/$name" "$plugin_root/runtime/$name" || fail "runtime copy differs"
done

for script in \
    "$plugin_root/scripts/inspect-agent-runtime.sh" \
    "$plugin_root/scripts/verify.sh"
do
    [ -f "$script" ] || fail "missing script"
    sh -n "$script" || fail "invalid shell script"
done

printf '%s\n' 'ai-workflow plugin verification: ok'
