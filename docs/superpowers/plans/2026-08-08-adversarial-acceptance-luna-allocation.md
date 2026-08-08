# 对抗式验收与 Luna 分流实施计划

> **For agentic workers:** execute one task at a time. Each task must have a named independent Terra xhigh adversarial reviewer before its result can feed the next task. Do not silently substitute a Sol reviewer.

**Goal:** Make the approved Terra/Luna construction allocation and adversarial acceptance ladder the default orchestration contract, while preserving frozen route/result/task wire formats and existing Plugin distribution guarantees.

**Architecture:** Keep `legacy`, `shadow`, and `enforced` wire modes. In the `terra_os` role policy, a verified construction envelope chooses either Luna max for deterministic bounded implementation or Terra xhigh for complex work. A separate append-only repair ledger makes reviewer/fixer identity, distinct-reviewer requirements, scoped findings, and terminal states enforceable. The Plugin mirrors the root runtime and presents the same contract.

**Tech Stack:** Python 3.11 standard library, TOML, JSON, `unittest`, POSIX shell, Git, Codex Plugin/Skill.

## Non-negotiable policy

- Terra xhigh is the resident construction OS: complex construction, integration, open-ended debugging, security/authorization/concurrency/persistence work, Luna task decomposition, and all ordinary task acceptance.
- Luna max is the preferred execution owner for an approved bounded envelope with exact allowed paths, deterministic `done_when`, L0/L1/L2 evidence, a negative or mutation check, and commands. It is never an automatic filler, planner, reviewer, security owner, or open-ended debugger.
- Sol medium does no ordinary task planning, construction, or local acceptance. It performs only final whole-project adversarial acceptance, except for the third-repair fallback described below.
- Sol xhigh authors an overall plan for large/cross-domain work and is the terminal fixer after the Sol-medium peer rejects the fallback repair. It receives no task-level post-fix acceptance.
- Terra medium and Sol high are not selectable default roles. `automatic_sol_high`, automatic xhigh selection, merge, and push stay disabled.
- An acceptance reviewer must act adversarially: seek counterexamples, inspect the diff and old contracts, run relevant commands, attempt a realistic negative/mutation case, and record evidence. A green test repeat alone is insufficient.

## Required task lifecycle

```text
approved owner (Luna max or Terra xhigh) → Terra xhigh adversarial review #1
                                           │ REWORK
                                           ▼
original owner repair → distinct Terra xhigh adversarial review #2
                                           │ REWORK
                                           ▼
Sol medium scoped repair → distinct Sol medium adversarial peer review
                                           │ REWORK
                                           ▼
Sol xhigh terminal scoped repair (no task-level review)

all terminal tasks → independent Sol medium whole-project adversarial acceptance
```

Every event is append-only and binds task id, owner/reviewer runtime identity, candidate/base commits, ordered finding IDs, allowed paths, verification and negative/mutation evidence, verdict, and terminal reason. A fixer cannot review its own work. Review #2 must be a fresh Terra identity distinct from both the owner and review #1. The Sol peer must be distinct from the Sol fallback fixer. Any missing identity/evidence fails closed.

## Task 1: Make construction allocation executable

**Owner:** Terra xhigh. **Local acceptance:** independent Terra xhigh adversarial reviewer.

**Files:**

- Modify: `config/ai_workflow.toml`
- Modify: `scripts/ai_workflow.py`
- Modify: `scripts/ai_workflow_routing.py`
- Modify: schemas/artifacts/runtime validators only where role acceptance requires it
- Modify/Create: focused routing, plan, and runtime tests

**Steps:**

- [ ] Write RED tests for role policy validation, approved Luna envelope eligibility, Luna fail-closed routing, Terra default for complex work, and rejection of Terra medium/Sol high/automatic escalation.
- [ ] Define a backward-compatible plan/envelope representation whose owner role and evidence requirements are validated before an effective Luna route is emitted; do not infer ownership from frozen route wire JSON.
- [ ] Route eligible bounded tasks to Luna max and all non-eligible construction tasks to Terra xhigh. Remove ordinary Sol medium supervisor/reviewer role injection from `terra_os` chains.
- [ ] Preserve exact legacy/shadow/enforced serialization and test all legacy roles plus new allowed roles through task/result/runtime/cost validators.
- [ ] Run focused tests, full discovery, compileall and diff check. Commit only this task’s scope.

**Adversarial acceptance minimum:** reviewer must try an empty/ambiguous envelope, an out-of-scope Luna request, a write/security task falsely labelled bounded, an implicit Terra-medium/Sol-high role, and a legacy wire mutation.

## Task 2: Enforce adversarial reviews and the capped repair ladder

**Owner:** Terra xhigh. **Local acceptance:** independent Terra xhigh adversarial reviewer.

**Files:**

- Modify: `scripts/ai_workflow.py`
- Modify: `scripts/ai_workflow_repairs.py`
- Modify/Create: `tests/test_ai_workflow_repairs.py` and focused integration tests

**Steps:**

- [ ] Replace the historical repair policy with immutable assignments: owner repair after review #1, then a new Terra-xhigh review #2; after that failure, one scoped Sol-medium fallback fix and a distinct Sol-medium peer review; peer failure creates one Sol-xhigh terminal-fix assignment with no task-level review.
- [ ] Add a verified repair-execution adapter rather than falling back to generic `run_until_gate`: bind assignment id, expected actor/runtime identity, candidate/base commit, allowed paths, opened findings and append-only ledger state before launch; record failed attempts once.
- [ ] Canonicalize terminal ledger records and reject stale/replayed/empty-finding/cross-task assignments. A completed/final state cannot be reopened by generic routing.
- [ ] Ensure terminal Sol-xhigh repair cannot silently acquire broader authority or trigger automatic merge/push; final overall Sol-medium acceptance remains required.
- [ ] Run focused/full/compile checks and commit only this task’s scope.

**Adversarial acceptance minimum:** reviewer must simulate self-review, reviewer reuse, wrong runtime identity, stale candidate, empty/new finding IDs, out-of-scope mutation, review #2 skip, Sol fallback self-peer, duplicate/replayed terminal assignment, and generic-runner bypass.

## Task 3: Luna-owned distribution and evidence work

**Owner:** Luna max, under a written bounded envelope. **Local acceptance:** independent Terra xhigh adversarial reviewer.

**Files:**

- Modify: `README.md`
- Modify: `plugins/ai-workflow/skills/orchestration/SKILL.md`
- Modify: `plugins/ai-workflow/skills/orchestration/agents/openai.yaml`
- Modify: `plugins/ai-workflow/config/ai_workflow.toml`
- Modify/Create: byte-identical runtime copies, verifier changes and `tests/test_ai_workflow_distribution.py`

**Steps:**

- [ ] Give Luna a path-bounded envelope that names exact source-to-plugin copy pairs, permitted documentation files, expected text assertions, parity commands and negative tamper check.
- [ ] Write RED tests for byte parity and published role/lifecycle language, then synchronize root runtime/config and Plugin copies without broadening executable authority.
- [ ] Update user-facing documentation to state Luna’s eligible work, Terra’s task acceptance ownership, Sol-medium final acceptance, and the bounded escalation path.
- [ ] Run Plugin and Skill validators, distribution tests, root full tests, compileall, shell syntax and `git diff --check`; commit only synchronized artifacts and tests.

**Adversarial acceptance minimum:** reviewer must tamper one mirrored file, look for stale prior policy language, invoke check/install validation in a temporary target, and confirm that documentation does not grant Luna review or ordinary Sol-medium roles.

## Task 4: Whole-project adversarial acceptance

**Owner:** Sol medium, distinct from any Task-2 fallback fixer or Task-3 Luna owner. This is the only normal Sol-medium acceptance.

**Evidence:** all task reports and commits; clean status; full unittest discovery; compileall; Plugin verification; Skill validation; shell syntax; distribution parity; diff check; role/lifecycle mutation results.

**Steps:**

- [ ] Inspect the complete cumulative diff and every task ledger, not only the latest commits.
- [ ] Re-run the complete gate from a clean checkout state and perform cross-task negative tests: Plugin/source drift, Luna review role, ordinary Sol-medium route, reviewer identity collision, invalid repair transition, and post-terminal generic fallback.
- [ ] Give `ACCEPT`, `REWORK`, or `BLOCKED` with concrete findings and evidence. If `REWORK` exposes a new cross-task issue, stop and request owner scope rather than restarting task-level repair loops.

## Completion gates

1. At least one eligible bounded implementation task is actually owned by Luna max and independently adversarially accepted by Terra xhigh.
2. Every task shows distinct owner/reviewer identities and an adversarial negative/mutation record; no task self-accepts.
3. The repair protocol permits exactly the approved ladder and no generic bypass, third Terra repair, extra Sol-medium repair, automatic Sol-high/Terra-medium selection, automatic merge, or automatic push.
4. Root and Plugin are byte-identical where distribution requires it, all verification is green, and a fresh Sol-medium whole-project adversarial acceptance is recorded.
