# Terra OS Default Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Terra medium the default execution owner, Sol medium the light-task reviewer and final bounded fallback fixer, Sol xhigh the owner-authorized large-project planner, and Luna an explicitly requested bounded evidence tool.

**Architecture:** Add a runtime role-policy layer beside legacy/shadow/enforced routing without changing the frozen wire modes. Add an append-only two-round Terra repair protocol plus one post-budget Sol medium fallback, then mirror repository runtime/config into the Plugin. Existing owner authorization, Git guards, candidate pinning and runtime identity remain authoritative.

**Tech Stack:** Python 3.11+ standard library, TOML, JSON, `unittest`, POSIX shell, Git, Codex Plugin and Skill files.

## Global Constraints

- Default role policy is `terra_os`; `legacy` is explicit compatibility and `shadow` never changes the current call chain.
- Terra `gpt-5.6-terra/medium` owns normal implementation, integration, debugging, recovery and repair rounds 1 and 2.
- Sol `gpt-5.6-sol/medium` owns light-task review. Only if the second Terra repair is still rejected does the original reviewer own the registered open findings, then a distinct Sol medium peer reviews.
- Sol `gpt-5.6-sol/xhigh` is an owner-authorized large-project planner only. Sol high has no scheduled role; `automatic_sol_high=false` and `automatic_xhigh=false` remain unchanged.
- Luna `gpt-5.6-luna/max` runs only as an explicitly planned L0/L1/L2 bounded evidence tool.
- After two Terra repairs, there is at most one original-reviewer Sol medium fix and one distinct Sol medium review; then work blocks. No self-review, third Terra repair, second Sol direct fix, merge, push or worktree deletion.
- Existing `ai-task-1`, `ai-result-1`, and `ai-route-decision-1` wire fields stay backward compatible.
- Runtime uses only the Python standard library; Plugin runtime/config copies stay byte-identical.

## File Structure

- `config/ai_workflow.toml`: default role policy and repair constants.
- `scripts/ai_workflow_routing.py`: Terra OS role-chain selection.
- `scripts/ai_workflow_repairs.py`: immutable repair ownership and round transitions.
- `scripts/ai_workflow.py`: public integration and append-only events.
- `tests/test_ai_workflow_terra_os.py`: role-policy complement tests.
- `tests/test_ai_workflow_repairs.py`: repair ownership and peer-review tests.
- `plugins/ai-workflow/`: byte-identical release copies and orchestration Skill.
- `README.md`: user-facing defaults, compatibility and recovery.

### Task 1: Deterministic Terra OS Role Policy

**Files:**
- Modify: `config/ai_workflow.toml`
- Modify: `scripts/ai_workflow_routing.py`
- Modify: `scripts/ai_workflow.py`
- Create: `tests/test_ai_workflow_terra_os.py`

**Interfaces:**
- Produces: `ROLE_POLICIES = {"legacy", "terra_os"}`; `roles_for_policy(task, request, route_name, policy) -> tuple[str, ...]`; `resolve_role_policy(config, override=None) -> str`.
- Preserves: `decide_route(..., mode)` wire values `legacy|shadow|enforced` and `RuntimeRouteDecision` serialization.

- [ ] **Step 1: Write failing tests** for default `terra_os`, normal write `("terra", "sol_reviewer")`, risky write `("sol_planner", "terra", "sol_reviewer")`, planning-only Sol, and zero implicit Luna roles.
- [ ] **Step 2: Run RED:** `/Users/lee/.local/bin/python3.11 -m unittest tests.test_ai_workflow_terra_os -v`; expect missing policy/functions or old Luna-first chains.
- [ ] **Step 3: Add exact TOML:** set `[routing] mode="enforced"`, `role_policy="terra_os"`; add `[repair] terra_max_rounds=2`, `round_1_fixer="terra"`, `round_2_fixer="terra"`, `post_terra_fixer="original_sol_medium_reviewer"`, `post_terra_reviewer="distinct_sol_medium_peer"`; keep all automatic authority flags false.
- [ ] **Step 4: Implement closed policy selection:** direct/blocked use no model; light semantic review uses Sol medium; normal writes use Terra medium then Sol medium reviewer; only an owner-authorized large-project request adds Sol xhigh planner first. Sol high is never selected. Luna is scheduled only by a validated plan task with `owner_role="luna"` and L0/L1/L2 evidence.
- [ ] **Step 5: Add complement tests:** unknown policy fails closed; legacy chains stay unchanged; shadow keeps legacy effective roles; wire JSON contains no `role_policy`; automatic xhigh/merge/push stay false.
- [ ] **Step 6: Run focused/full:** `/Users/lee/.local/bin/python3.11 -m unittest tests.test_ai_workflow_terra_os tests.test_ai_workflow_routing_v2 -v && /Users/lee/.local/bin/python3.11 -m unittest discover -s tests -v`.
- [ ] **Step 7: Commit:** `git add config/ai_workflow.toml scripts/ai_workflow.py scripts/ai_workflow_routing.py tests/test_ai_workflow_terra_os.py && git commit -m "feat: make Terra OS the default role policy"`.

### Task 2: Two-Round Terra Repair and Sol Medium Fallback

**Files:**
- Create: `scripts/ai_workflow_repairs.py`
- Create: `tests/test_ai_workflow_repairs.py`
- Modify: `scripts/ai_workflow.py`

**Interfaces:**
- Produces: `RepairAssignment`; `assign_repair(open_findings, round_number, original_reviewer, peer_reviewer) -> RepairAssignment`; `record_repair_assignment(store, task_id, assignment)`; `validate_repair_result(assignment, actor_identity, changed_paths)`.
- Events contain: `repair_round`, fixer/reviewer identities, finding IDs, base commit and candidate commit.

- [ ] **Step 1: Write failing tests:** rounds 1 and 2 assign Terra; only a rejected second review assigns the original Sol medium reviewer and requires a different Sol medium peer; another repair raises `REPAIR_BUDGET_EXHAUSTED`.
- [ ] **Step 2: Run RED:** `/Users/lee/.local/bin/python3.11 -m unittest tests.test_ai_workflow_repairs -v`; expect missing module/interfaces.
- [ ] **Step 3: Implement immutable assignments:** unique nonempty findings; Terra rounds exactly 1 or 2; post-budget peer required and distinct; never select Sol high or Sol xhigh as a repair/review actor.
- [ ] **Step 4: Enforce scope/self-review separation:** reject wrong fixer, peer equal to fixer, missing commits, new findings or out-of-scope paths.
- [ ] **Step 5: Append events:** reject replay and round 2 before round 1 completion. After a rejected round 2 only the original Sol medium may fix and only its distinct Sol medium peer may review; another rework verdict enters BLOCKED. A new Sol xhigh plan requires separate owner authorization and a newly scoped large-project plan.
- [ ] **Step 6: Run focused/full/compile:** `/Users/lee/.local/bin/python3.11 -m unittest tests.test_ai_workflow_repairs -v && /Users/lee/.local/bin/python3.11 -m unittest discover -s tests -v && /Users/lee/.local/bin/python3.11 -m compileall -q scripts tests`.
- [ ] **Step 7: Commit:** `git add scripts/ai_workflow.py scripts/ai_workflow_repairs.py tests/test_ai_workflow_repairs.py && git commit -m "feat: cap Terra repair handoffs"`.

### Task 3: Plugin, Skill, and Documentation Synchronization

**Files:**
- Modify: `README.md`
- Modify: `plugins/ai-workflow/skills/orchestration/SKILL.md`
- Modify: `plugins/ai-workflow/skills/orchestration/agents/openai.yaml`
- Modify: `plugins/ai-workflow/config/ai_workflow.toml`
- Modify: `plugins/ai-workflow/runtime/ai_workflow.py`
- Modify: `plugins/ai-workflow/runtime/ai_workflow_routing.py`
- Create: `plugins/ai-workflow/runtime/ai_workflow_repairs.py`
- Modify: `plugins/ai-workflow/scripts/verify.sh`
- Modify: `tests/test_ai_workflow_distribution.py`

**Interfaces:**
- Produces: default `$ai-workflow:orchestration` behavior and byte-identical release copies.

- [ ] **Step 1: Write failing content/parity tests:** Skill names Terra medium as implementation owner, limits Luna, documents two Terra repairs then one Sol medium fallback, reserves Sol xhigh for owner-approved large-project planning, and verifier includes the repair module.
- [ ] **Step 2: Run RED:** `/Users/lee/.local/bin/python3.11 -m unittest tests.test_ai_workflow_distribution -v`.
- [ ] **Step 3: Update Skill/UI:** perform installer check, default to `terra_os`, distinguish native Luna from exec role contracts, enforce two Terra rounds then one Sol medium fallback and stop thereafter; keep the Skill concise.
- [ ] **Step 4: Update README:** document role hierarchy, default/legacy/shadow, two Terra repairs, final original-Sol-medium fallback, distinct Sol medium peer review, owner-only large-project xhigh planning, install/check/uninstall and new-task restart boundary.
- [ ] **Step 5: Sync/validate:** run Plugin validator, Skill quick validator, distribution/full tests, compileall, shell syntax, `verify.sh` and `git diff --check`.
- [ ] **Step 6: Commit:** `git add README.md plugins/ai-workflow tests/test_ai_workflow_distribution.py && git commit -m "docs: ship the Terra OS orchestration contract"`.

### Task 4: Independent Mutations and Protocol Acceptance

**Files:**
- Modify: `tests/test_ai_workflow_terra_os.py`
- Modify: `tests/test_ai_workflow_repairs.py`
- Evidence only: `.superpowers/sdd/2026-08-08-terra-os-default/`

**Interfaces:**
- Produces: deterministic gate evidence and a fresh same-grade Sol peer verdict.

- [ ] **Step 1: Run complete gate:** clean status, full unittest, compileall, Plugin verifier and `git diff --check`.
- [ ] **Step 2: Run seven mutations:** insert Luna into normal writes; replace Terra with Luna; assign Sol to Terra round 2; allow a third Terra round; allow Sol fixer self-review; allow automatic Sol high/xhigh; omit post-budget peer identity. Each named test must fail, then restore and pass.
- [ ] **Step 3: Fake closure:** normal write, authorized large-project write, explicit Luna tool, Terra rounds 1/2, original-Sol-medium post-budget fix, distinct-peer acceptance and post-fallback block.
- [ ] **Step 4: Fresh Sol medium peer review:** reviewer must be the same grade as acceptance but not the post-budget fixer; provide spec, plan, diff, tests and mutations; verdict is acceptance, rework or rethink recommendation only.
- [ ] **Step 5: Commit test additions:** `git add tests/test_ai_workflow_terra_os.py tests/test_ai_workflow_repairs.py && git commit -m "test: verify Terra OS repair handoffs"`.

## Plan Self-Review Results

- **Spec coverage:** default hierarchy, legacy/shadow, explicit Luna tools, two-round Terra repair, Sol medium fallback, peer review, large-project xhigh planning gate, distribution and mutations map to Tasks 1–4.
- **Placeholder scan:** no deferred markers or unspecified test steps remain.
- **Type consistency:** `terra_os`, `RepairAssignment`, repair fields and role names are consistent.
- **Safety:** no automatic Sol high/xhigh, merge, push, worktree deletion, schema break or third-party runtime dependency.
