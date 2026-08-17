# Native Luna Max Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the native `gpt-5.6-luna/max` subagent the only default Luna execution surface and remove the repository’s custom Agent from the active workflow.

**Architecture:** Runtime contracts keep the stable Workflow role `luna`, but native evidence no longer depends on a custom `agent_type` string. The controller verifies model, reasoning effort, native metadata, thread identity, permissions and cwd. Templates are removed from active distribution; lifecycle code remains only as a migration-cleanup utility and is not required by preflight.

**Tech Stack:** Python 3.11 `unittest`, JSON Schema, POSIX shell, TOML, no new dependencies.

## Global Constraints

- Native Luna means `NATIVE_SUBAGENT` with observed model `gpt-5.6-luna` and reasoning effort `max`.
- Missing or conflicting native model/effort/identity evidence is fail-closed as `BLOCKED`.
- `CODEX_EXEC_ROLE_CONTRACT` remains a distinct surface and cannot claim native identity.
- The stable Workflow role remains `luna`; no `luna_worker` or custom `luna_max` agent is a default selectable identity.
- Do not delete user files under `/Users/lee/.codex`; the current user agent directory was verified empty.
- Keep migration cleanup available for already-installed historical templates, without requiring a current template file.
- Root and Plugin runtime/schema copies must remain byte-identical.

---

### Task 1: Lock native runtime identity

**Files:**
- Modify: `scripts/ai_workflow_runtime.py`
- Modify: `scripts/ai_workflow_artifacts.py`
- Modify: `config/ai_workflow_runtime_evidence.schema.json`
- Modify: `tests/test_ai_workflow_runtime.py`
- Modify: `tests/test_ai_workflow_artifacts.py`
- Modify: native runtime fixtures under `tests/fixtures/runtime/`

- [ ] **Step 1: Write failing tests** for a native observation with `agent_type=None` and correct `gpt-5.6-luna/max`, plus rejection of wrong model/effort and missing native model/effort.
- [ ] **Step 2: Run focused tests** and confirm the old non-null `agent_type` requirement fails for the native case.
- [ ] **Step 3: Update runtime/schema validators** so native identity is model/effort/surface/UUID/evidence based; keep exec agent type null and reject custom-name spoofing.
- [ ] **Step 4: Run runtime/artifact focused tests** and confirm all native/exec negative cases pass.
- [ ] **Step 5: Commit** with `fix(runtime): verify native Luna model identity`.

### Task 2: Remove custom Agent from the active distribution

**Files:**
- Delete: `.codex/agents/luna-max.toml`
- Delete: `plugins/ai-workflow/agents/luna-max.toml`
- Modify: `plugins/ai-workflow/scripts/verify.sh`
- Modify: `plugins/ai-workflow/skills/orchestration/SKILL.md`
- Modify: `plugins/ai-workflow/skills/orchestration/agents/openai.yaml`
- Modify: `README.md`
- Modify: `tests/test_ai_workflow_distribution.py`

- [ ] **Step 1: Add failing distribution assertions** requiring no active custom template, no install preflight, native-subagent default wording, and migration cleanup explicitly marked non-default.
- [ ] **Step 2: Run distribution tests** and confirm they fail against the current template/preflight contract.
- [ ] **Step 3: Remove active template checks and default install instructions**; keep lifecycle scripts only as an explicit historical cleanup path.
- [ ] **Step 4: Run distribution tests and Plugin verifier**.
- [ ] **Step 5: Commit** with `feat(workflow): make native Luna the default`.

### Task 3: Synchronize and verify

**Files:**
- Modify: Plugin runtime/schema mirrors as required by Tasks 1–2.
- Modify: `tests/test_ai_workflow_adversarial_acceptance.py` and other runtime fixtures only where native `agent_type` is currently spoofed.

- [ ] **Step 1: Replace native fixture `agent_type=luna_max` with nullable native metadata and preserve explicit legacy-name rejection tests as negative cases.**
- [ ] **Step 2: Run full unittest discovery, compileall, shell syntax, Plugin verifier, Skill validator, root/Plugin parity and `git diff --check`.**
- [ ] **Step 3: Record that the user-level custom directory was empty and no user file was deleted.**
- [ ] **Step 4: Commit the synchronized change and hand off the branch for local merge or PR.**
