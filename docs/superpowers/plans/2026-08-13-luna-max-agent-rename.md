# Luna Max Agent Rename Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate the project custom Agent identity from `luna_worker` and
`luna-worker.toml` to `luna_max` and `luna-max.toml`, with a fail-closed,
data-preserving installer migration for verified existing installations.

**Architecture:** Keep the workflow role `luna` unchanged while replacing the
custom-agent identity used by native runtime evidence and distribution assets.
The installer treats verified legacy files as one-time migration input; no new
route or runtime accepts the legacy identity.  Root and Plugin copies remain
byte-identical wherever the existing verifier requires them.

**Tech Stack:** Python 3.11 standard library, TOML/JSON, POSIX shell, Git,
`unittest`.

## Global Constraints

- Canonical custom-agent ID is `luna_max`; human-facing name is **Luna Max**.
- Canonical filenames are `luna-max.toml`, `.ai-workflow-luna-max.state`, and
  `.ai-workflow-luna-max.backup`.
- Keep workflow role fields `luna` and `luna_construction` unchanged.
- Pin the agent to `gpt-5.6-luna` and `model_reasoning_effort = "max"`.
- Old `luna_worker` files are migration input only; new native execution must
  reject old agent identity with `RUNTIME_IDENTITY_CONFLICT`.
- Keep L0/L1/L2 envelopes, role allocation, retry ladder, cost evidence,
  append-only receipts, and no automatic merge/push unchanged.
- Preserve user-owned/unsafe/ambiguous legacy files; all such cases fail
  closed with no partial canonical publication.
- Use test-first changes and keep all required root/Plugin copies byte-identical.

---

### Task 1: Canonical Luna Max templates and native identity contract

**Owner:** Luna max — exact template, fixture, and bounded test work.

**Files:**
- Rename: `.codex/agents/luna-worker.toml` → `.codex/agents/luna-max.toml`
- Rename: `plugins/ai-workflow/agents/luna-worker.toml` →
  `plugins/ai-workflow/agents/luna-max.toml`
- Modify: `tests/test_ai_workflow_runtime.py`
- Modify: `tests/fixtures/runtime/{one,two,missing,conflict,nonstring-duplicate}/*`

**Interfaces:**
- Native runtime expectation: `agent_type == "luna_max"` for
  `NATIVE_SUBAGENT`.
- Exec runtime expectation: `agent_type is None` for
  `CODEX_EXEC_ROLE_CONTRACT`.
- `luna_worker` remains an invalid native identity after migration.

- [ ] **Step 1: Add failing runtime and template tests**

  In `RuntimeIdentityTest`, make native expected/observed fixtures require
  `luna_max` and add a direct old-alias regression:

  ```python
  def test_native_luna_worker_alias_is_rejected(self):
      observed = runtime_observation(agent_type="luna_worker")
      with self.assertRaisesRegex(workflow.WorkflowError,
                                  "RUNTIME_IDENTITY_CONFLICT"):
          workflow.verify_runtime_identity(runtime_expected(), observed)
  ```

  In `DistributionContractTest`, require the renamed root and release templates
  to be byte-identical, parse `name == "luna_max"`, and fail if the old
  template is the canonical release artifact.

- [ ] **Step 2: Run the focused tests to establish RED**

  Run:

  ```bash
  /Users/lee/.local/bin/python3.11 -m unittest -v \
    tests.test_ai_workflow_runtime.RuntimeIdentityTest \
    tests.test_ai_workflow_distribution.DistributionContractTest
  ```

  Expected: failures because templates and rollout fixtures still use
  `luna_worker`/`luna-worker.toml`.

- [ ] **Step 3: Publish the canonical template and update runtime fixtures**

  Rename both templates and set:

  ```toml
  name = "luna_max"
  description = "Luna Max：处理由主代理明确委派的、范围有限..."
  ```

  Update the native fixture `agent_type` fields and runtime test helpers to
  `luna_max`.  Retain one literal old alias only in the negative test.

- [ ] **Step 4: Verify GREEN and mutation coverage**

  Run the focused command again, then temporarily mutate a native fixture back
  to `luna_worker` in a disposable copy and confirm native verification rejects
  it.  Restore the fixture.

- [ ] **Step 5: Commit the bounded canonical-identity change**

  ```bash
  git add .codex/agents/luna-max.toml plugins/ai-workflow/agents/luna-max.toml \
    tests/test_ai_workflow_runtime.py tests/fixtures/runtime
  git rm .codex/agents/luna-worker.toml plugins/ai-workflow/agents/luna-worker.toml
  git commit -m "feat(workflow): rename native Luna agent to luna max"
  ```

### Task 2: Transactional legacy-install migration

**Owner:** Terra xhigh — lifecycle state changes are filesystem-transaction
and rollback-sensitive.

**Files:**
- Modify: `plugins/ai-workflow/scripts/agent_lifecycle.py`
- Modify: `plugins/ai-workflow/scripts/verify.sh`
- Modify: `tests/test_ai_workflow_distribution.py`

**Interfaces:**
- `TARGET_FILENAME = "luna-max.toml"`.
- `STATE_FILENAME = ".ai-workflow-luna-max.state"`.
- `BACKUP_FILENAME = ".ai-workflow-luna-max.backup"`.
- Legacy migration input comprises only verified old template bytes at
  `luna-worker.toml`, optionally with a verified old ownership state and
  backup.  A conflicting old/new combination returns nonzero without writes.

- [ ] **Step 1: Add failing lifecycle migration tests**

  Add real temporary-directory cases to `AgentLifecycleTest`:

  ```python
  def test_verified_luna_worker_install_migrates_to_luna_max_atomically(self):
      write_verified_legacy_install(target)
      self.assertEqual(0, self.install(target).returncode)
      self.assertFalse((target / "luna-worker.toml").exists())
      self.assertTrue((target / "luna-max.toml").exists())
      self.assertTrue((target / ".ai-workflow-luna-max.state").exists())

  def test_legacy_and_canonical_entries_fail_closed(self):
      write_verified_legacy_install(target)
      (target / "luna-max.toml").write_bytes(b'user owned')
      self.assertNotEqual(0, self.install(target).returncode)
      self.assertEqual(b'user owned', (target / "luna-max.toml").read_bytes())
  ```

  Cover legacy-with-state/backup, unverified legacy bytes, legacy symlink,
  interrupted publish rollback, `--check`, and uninstall after migration.

- [ ] **Step 2: Run migration tests to establish RED**

  Run:

  ```bash
  /Users/lee/.local/bin/python3.11 -m unittest -v \
    tests.test_ai_workflow_distribution.AgentLifecycleTest
  ```

  Expected: canonical names do not exist and legacy migration cases fail.

- [ ] **Step 3: Implement identity-bound legacy migration**

  In `agent_lifecycle.py`:

  1. set canonical constants and template digest from `luna-max.toml`;
  2. add explicit legacy filename/state/backup constants and a fixed SHA-256
     of the old release template;
  3. classify canonical and legacy paths through the same no-follow descriptor
     primitives; reject both-name and malformed combinations;
  4. retire verified legacy agent/state/backup into tombstones, publish staged
     canonical agent/state/backup without clobbering, and restore original
     identities on every failed publish/hook path;
  5. make `--check` return success only for a complete canonical owned install;
  6. let uninstall remove only a complete canonical owned installation and
     restore a verified user backup as before.

  Update `verify.sh` to compare only canonical `luna-max.toml` root/Plugin
  templates and require no old release template.

- [ ] **Step 4: Run focused lifecycle and tamper verification**

  Run lifecycle tests, `sh plugins/ai-workflow/scripts/verify.sh`, then copy
  the release to a temporary directory, alter `luna-max.toml`, and verify the
  release verifier exits nonzero without changing the repository.

- [ ] **Step 5: Commit the transaction-safe migration**

  ```bash
  git add plugins/ai-workflow/scripts/agent_lifecycle.py \
    plugins/ai-workflow/scripts/verify.sh tests/test_ai_workflow_distribution.py
  git commit -m "feat(plugin): migrate legacy Luna worker installs"
  ```

### Task 3: Published Luna Max contract and distribution closure

**Owner:** Luna max — exact documentation, metadata, mirror, and release-test
work.  It may not change route, repair, or lifecycle production semantics.

**Files:**
- Modify: `README.md`
- Modify: `plugins/ai-workflow/skills/orchestration/SKILL.md`
- Modify: `plugins/ai-workflow/skills/orchestration/agents/openai.yaml`
- Modify: `tests/test_ai_workflow_distribution.py`
- Modify only if changed by Task 1: root/Plugin runtime mirrors and runtime
  fixture lists required by the existing byte-parity verifier.

**Interfaces:**
- Published native custom Agent command/configuration identity is `luna_max`.
- Published label is Luna Max.
- Old `luna_worker` is mentioned only in the explicit installer migration
  section, never as a selectable current Agent.

- [ ] **Step 1: Add failing published-contract tests**

  Extend `test_published_role_and_lifecycle_language_has_no_legacy_allocation`
  to exercise the published files as one contract:

  ```python
  self.assertIn("luna_max", published)
  self.assertIn("luna max", published)
  self.assertNotRegex(published, r"(?:require|invoke|select).*luna_worker")
  ```

  Add a migration-specific assertion allowing old spelling only adjacent to
  `migration`/`迁移` and requiring it not be presented as an execution role.

- [ ] **Step 2: Run the focused distribution contract to establish RED**

  Run:

  ```bash
  /Users/lee/.local/bin/python3.11 -m unittest -v \
    tests.test_ai_workflow_distribution.DistributionContractTest
  ```

  Expected: reader-facing files still direct users to `luna_worker`.

- [ ] **Step 3: Synchronize reader-facing contract text**

  Update README and orchestration Skill to say **Luna Max** / `luna_max`,
  document the one-time installer migration from `luna_worker`, and retain all
  existing bounded-envelope/no-review/no-acceptance language.  Update metadata
  description/prompt if it contains the old selectable name.  Do not modify
  historical plans/specifications that record the old identifier.

- [ ] **Step 4: Verify full distribution behavior**

  Run:

  ```bash
  /Users/lee/.local/bin/python3.11 -m unittest -v tests.test_ai_workflow_distribution
  sh plugins/ai-workflow/scripts/verify.sh
  python3 /Users/lee/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
    plugins/ai-workflow/skills/orchestration
  ```

  Verify a temporary target performs install → check → uninstall with canonical
  filenames and no old template left behind.

- [ ] **Step 5: Commit the published contract update**

  ```bash
  git add README.md plugins/ai-workflow/skills/orchestration/SKILL.md \
    plugins/ai-workflow/skills/orchestration/agents/openai.yaml \
    tests/test_ai_workflow_distribution.py
  git commit -m "docs(workflow): publish Luna Max agent identity"
  ```

### Task 4: Independent adversarial acceptance and project closure

**Owner:** Terra xhigh for task-scoped adversarial review; Sol medium for one
whole-project final acceptance.

**Files:**
- Read-only review of all tracked changes since `d71c1e4`.
- Write ignored evidence only under `.superpowers/sdd/`.

- [ ] **Step 1: Perform a Terra xhigh adversarial review**

  Independently tamper a copied template, attempt old/native identity
  injection, legacy+canonical file conflict, migrated uninstall, state/backup
  path race, root/Plugin drift, and stale documentation invocation.  Report
  concrete Critical/Important findings only.

- [ ] **Step 2: Apply the approved capped repair ladder if review fails**

  First failed review returns to the original owner; second failed review is
  rechecked by a distinct Terra xhigh; only then use the existing scoped
  Sol-medium repair/different-peer/Sol-xhigh-terminal sequence.  Do not add a
  third Terra repair.

- [ ] **Step 3: Run final technical gates**

  Run:

  ```bash
  /Users/lee/.local/bin/python3.11 -m unittest discover -s tests -v
  /Users/lee/.local/bin/python3.11 -m compileall -q config scripts tests plugins/ai-workflow
  sh plugins/ai-workflow/scripts/verify.sh
  for file in plugins/ai-workflow/scripts/*.sh; do sh -n "$file"; done
  git diff --check
  ```

- [ ] **Step 4: Obtain independent Sol-medium whole-project acceptance**

  Verify the actual default role allocation is unchanged, the old identifier
  cannot become a new runtime identity, the migration preserves user data,
  documentation uses Luna Max, all root/Plugin copies are synchronized, and
  no merge/push occurred.  Record the verdict in the plan evidence workspace.

- [ ] **Step 5: Commit only if the acceptance repair ladder changed tracked files**

  Use a Conventional Commit scoped to the repaired component.  Do not merge
  or push automatically.

## Plan self-review

- Spec coverage: Tasks 1–3 cover canonical identifier/template, runtime
  identity, installer migration, distribution, and documentation; Task 4
  covers adversarial and final acceptance.
- Scope: filesystem transaction work is isolated in Task 2; Luna Max work is
  bounded to templates/tests/docs in Tasks 1 and 3.
- Compatibility: historical plans are explicitly excluded from rewrite; the
  legacy identifier is accepted only at the installer migration boundary.
- Type consistency: `luna_max` is the custom-agent identity throughout;
  workflow role remains `luna`.
