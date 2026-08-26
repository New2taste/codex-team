# Live Runtime Contract Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the real Codex CLI rollout and honest task-id echo pass the existing fail-closed live runtime chain.

**Architecture:** Parse real rollout JSONL as a complete stream and normalize only known CLI 0.150 shapes into the existing runtime allowlist. Share one copy-based identity normalization helper between normal and repairs ingestion while leaving scheduler validation unchanged.

**Tech Stack:** Python 3.11 standard library, POSIX shell, jq, unittest.

**Spec:** `docs/superpowers/specs/2026-08-26-live-runtime-contract-design.md`

## Global Constraints

- Preserve the generic inspector failure message and never echo rollout input.
- Unknown or conflicting runtime shapes fail closed.
- Only an exact controller-bound task-id echo can normalize partial-null identity.
- Mirror runtime files through `scripts/sync_plugin.py`; do not hand-edit runtime mirrors.
- No scheduler behavior changes.

---

### Task 1: S3 real rollout fixture and inspector

**Files:**
- Create: `tests/fixtures/runtime/codex-0.150-real/rollout-019fc73c-4d40-7c20-a82a-c5a9ae078bcf.jsonl`
- Modify: `tests/test_ai_workflow_runtime.py`
- Modify: `plugins/ai-workflow/scripts/inspect-agent-runtime.sh`

**Interfaces:**
- Consumes: an absolute sessions directory and one thread UUID.
- Produces: the existing eight-field runtime observation JSON.

- [ ] **Step 1: Add the redacted real JSONL fixture**

Include at least a `session_meta` record with `payload.id`,
`payload.session_id`, `payload.cwd`, and records carrying model, effort,
`sandbox_policy={"type":"read-only"}`, and managed restricted
`permission_profile`. Record CLI version `0.150.0-alpha.8`; remove prompts,
tokens, account IDs, and environment values.

- [ ] **Step 2: Write failing positive and negative tests**

Add tests that require the real fixture to succeed and normalize both policy
objects to `"read-only"`. Derive mutations for missing session identity,
conflicting models, unknown sandbox type, unknown permission shape, malformed
JSONL, and duplicate matching files; each must fail without leaking sentinels.

- [ ] **Step 3: Verify RED**

Run:

```bash
python3.11 -m unittest \
  tests.test_ai_workflow_runtime.RuntimeInspectorTest -v
```

Expected: the real-format positive test fails with inspector exit 2.

- [ ] **Step 4: Implement complete-stream jq parsing**

Use `jq -cs` so the input is one array. Add closed helpers that:

- collect unique non-empty strings across the stream;
- derive thread id from `payload.id`, `payload.session_id`, or existing
  `thread_id`, requiring one value equal to the requested UUID;
- normalize only string policies or the exact known object shapes;
- keep nullable agent type and optional native-agent binding;
- emit only the existing eight fields.

- [ ] **Step 5: Verify GREEN**

Run the focused test above, then:

```bash
python3.11 -m unittest tests.test_ai_workflow_runtime -v
```

Expected: all runtime tests pass.

### Task 2: S4 bound task-id echo normalization

**Files:**
- Modify: `tests/test_ai_workflow.py`
- Modify: `tests/test_ai_workflow_adversarial_acceptance.py`
- Modify: `tests/test_ai_workflow_scheduler.py`
- Modify: `scripts/ai_workflow.py`
- Modify: `scripts/ai_workflow_repairs.py`
- Mirror: `plugins/ai-workflow/runtime/ai_workflow.py`
- Mirror: `plugins/ai-workflow/runtime/ai_workflow_repairs.py`

**Interfaces:**
- Produces: `normalize_result_identity(result, *, expected_task_id, error_code) -> Mapping[str, object]`.
- `validate_role_result(..., *, expected_task_id: str | None = None) -> None`.
- `_v2_validate_controller_result(..., *, expected_task_id: str | None = None)`.

- [ ] **Step 1: Write failing normal-ingestion tests**

Cover exact bound echo accepted, wrong task id rejected, missing
`expected_task_id` rejected, non-null attempt rejected, all-null still
accepted, caller mapping unchanged, and raw attempt artifact unchanged.

- [ ] **Step 2: Write failing repairs and scheduler tests**

Apply the same matrix to `_v2_validate_controller_result`. Add a scheduler
test proving the bound echo is still rejected by its earlier exact dispatch
identity check.

- [ ] **Step 3: Verify RED**

Run the named tests. Expected: exact bound echo is rejected as partially null.

- [ ] **Step 4: Implement the shared copy-based helper**

Accept only:

- all four identity values null; or
- dispatch/step/attempt null and task id exactly equal to a non-empty
  controller-supplied expected id.

Reject every other partially-null shape with the caller-selected error code.
Pass the bound task id from `AttemptAccountingContext` and the stored
assignment task; never derive it from result content.

- [ ] **Step 5: Add prompt instruction**

Add one stable line to normal role and repairs assignment prompts:
`Set dispatch_id, task_id, step_id, and attempt to null; controller identity is reserved.`
Update prompt assertions without changing task-contract serialization.

- [ ] **Step 6: Sync mirrors and verify GREEN**

```bash
python3.11 scripts/sync_plugin.py --write
python3.11 -m unittest \
  tests.test_ai_workflow \
  tests.test_ai_workflow_adversarial_acceptance \
  tests.test_ai_workflow_scheduler -v
python3.11 scripts/sync_plugin.py --check
```

Expected: all pass and mirrors are byte-identical.

### Task 3: Joint verification and real live gate

**Files:**
- No production changes unless a new failing test demonstrates a defect.

- [ ] **Step 1: Run full verification**

```bash
sh scripts/verify_all.sh
```

- [ ] **Step 2: Run one authorized real Team Call L1 probe**

Use the embedded Codex CLI, an external temporary state root, and the real
sessions directory.

- [ ] **Step 3: Check the five joint-gate conditions**

Require exit 0, runtime evidence present, result accepted, raw attempt keeping
its wire identity values, and a clean repository.
