# Resident Router Research Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a shadow-only offline experiment that measures whether Luna, Sol, or Terra benefits from a stable warm prompt prefix enough to justify router overhead.

**Architecture:** Keep production routing untouched. A standalone probe module validates a frozen manifest, executes closed read-only arms through an injected executor, and writes atomic experiment artifacts; a separate analyzer computes cache/cost/quality gates.

**Tech Stack:** Python 3.11 standard library, JSON Schema documents, unittest.

**Spec:** `docs/superpowers/specs/2026-08-26-resident-router-research-design.md`

## Global Constraints

- No `WorkflowStore` import or task-store writes.
- No effective-route mutation; R4 is out of scope.
- Luna/Sol/Terra are the only model families.
- Live probes require explicit `--allow-live-model`; fake/dry-run is default.
- Experiment conclusions remain `OBSERVATION_ONLY` below 32 complete paired cases or with synthetic/unavailable evidence.
- Root/plugin config parity remains exact.

---

### Task 1: R1 manifest and cost-role contracts

**Files:**
- Create: `config/ai_workflow_router_probe_manifest.schema.json`
- Create mirror: `plugins/ai-workflow/config/ai_workflow_router_probe_manifest.schema.json`
- Modify: `config/ai_workflow.toml`
- Modify: `config/ai_workflow_cost_evidence.schema.json`
- Modify: `scripts/ai_workflow_costs.py`
- Modify: `scripts/sync_plugin.py`
- Test: `tests/test_ai_workflow_router_probe.py`
- Test: `tests/test_sync_plugin.py`

**Interfaces:**
- Produces: closed arm IDs, cache conditions, and `router_probe` as a cost-evidence-only role.

- [ ] **Step 1: Write failing contract tests**

Assert the manifest requires exact fields and rejects unknown arm/model,
unknown cache condition, extra properties, mismatched model/arm family,
invalid hashes, and relative experiment roots. Assert cost normalization
accepts `router_probe` but production `ROLES` does not.

- [ ] **Step 2: Verify RED**

```bash
python3.11 -m unittest tests.test_ai_workflow_router_probe -v
```

Expected: import/schema failures because the probe contract does not exist.

- [ ] **Step 3: Add minimal contracts**

Add `[router_probe] enabled=false`, a fixed prompt-template version, and the
three closed model/effort pairs. Add `router_probe` only to the dedicated
cost-evidence role allowlist, not to production routing roles. Add the manifest
schema to `sync_plugin.CONFIG_FILES`.

- [ ] **Step 4: Create the initial generated plugin schema copy**

Copy the new root schema byte-for-byte into plugin config, then use
`sync_plugin.py` for subsequent synchronization.

- [ ] **Step 5: Verify GREEN**

Run focused tests plus `python3.11 scripts/sync_plugin.py --check`.

### Task 2: R2 offline probe runner

**Files:**
- Create: `scripts/ai_workflow_router_probe.py`
- Test: `tests/test_ai_workflow_router_probe.py`
- Create: `tests/fixtures/router-probe/cases.json`

**Interfaces:**
- `load_probe_manifest(path: Path) -> dict[str, object]`
- `build_probe_prompt(case, *, template_version: str) -> str`
- `run_probe_batch(manifest, *, executor, output_root: Path) -> dict[str, object]`
- `ProbeExecutor.run(model, effort, prompt) -> ProbeAttempt`

- [ ] **Step 1: Write failing validator and prompt tests**

Require byte-identical fixed prefixes across cases, variable intake only after
the prefix separator, deterministic case ordering from the recorded seed, and
closed model/effort pairs.

- [ ] **Step 2: Write failing artifact-safety tests**

Require an absolute experiment root outside `.git`, atomic write-once batch
artifacts, no symlink/hardlink destination, no `WorkflowStore` import, and
rejection of a reused attempt ID.

- [ ] **Step 3: Write failing fake-executor tests**

Given deterministic usage events, require one manifest row and one normalized
`cost-evidence-1` row per case/arm with correct prompt bytes, token counts,
cache condition, and paired-case binding.

- [ ] **Step 4: Verify RED**

Run the focused module tests and confirm missing APIs fail.

- [ ] **Step 5: Implement the minimal standard-library runner**

Default CLI mode is `--dry-run`; `--runner fake` uses fixture usage; live mode
is present only behind `--allow-live-model` and is not exercised before S3/S4
joint exit 0. The module must not import or call production routing/state code
except pure usage and cost-normalization helpers.

- [ ] **Step 6: Verify GREEN**

Run all router-probe tests and a dry-run against the fixture.

### Task 3: R3 aggregation and decision report

**Files:**
- Modify: `scripts/ai_workflow_router_probe.py`
- Test: `tests/test_ai_workflow_router_probe.py`

**Interfaces:**
- `aggregate_probe_results(records, manifests) -> dict[str, object]`
- `evaluate_probe_decision(summary, *, minimum_cases=32) -> str`
- `render_probe_report(summary) -> str`

- [ ] **Step 1: Write failing measured-report tests**

Generate a deterministic 32-case, four-stratum measured matrix in the test.
Cover per-arm cold-start vs warm-tail cache ratio, uncached input, latency,
quality miss counts, and deterministic cache-mechanism candidate selection.

- [ ] **Step 2: Write failing fail-closed decision tests**

Require `OBSERVATION_ONLY` for fewer than 32 complete paired cases, synthetic
or unavailable evidence, prompt-prefix drift, missing arms, or cache readings
that cannot be reproduced. Require any P0 miss to eliminate an arm. Require
`KEEP_DETERMINISTIC_BASELINE` when no arm covers router overhead.

- [ ] **Step 3: Verify RED**

Run focused tests and confirm analysis APIs are missing.

- [ ] **Step 4: Implement aggregation and rendering**

Reuse cost normalization and paired-case vocabulary, but do not call or alter
the production optimization gate. Keep mechanism and quality sections
separate in the report.

- [ ] **Step 5: Verify GREEN and full suite**

```bash
python3.11 -m unittest tests.test_ai_workflow_router_probe -v
sh scripts/verify_all.sh
```

Expected: all tests and plugin parity pass.

### Task 4: Documentation handoff

**Files:**
- Modify: `docs/ARCHITECTURE.md`
- Modify: `README.md`

- [ ] **Step 1: Document shadow-only status**

State that the research runner cannot change effective routing and that live
integration R4 remains blocked on S3/S4 plus separate owner approval.

- [ ] **Step 2: Document commands**

Add dry-run/fake examples and the experiment-root safety requirement. Do not
publish a true cost winner before measured data, a dated rate snapshot, and
downstream counterfactual cost exist.

- [ ] **Step 3: Run full verification**

```bash
sh scripts/verify_all.sh
```
