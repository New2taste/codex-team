# Lifecycle Transaction Escalation Remediation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the owner-authorized, Sol xhigh-frozen `TRANSACTION_EXCEPTION_ROLLBACK` defect so Plugin Agent installation never leaves a newly published Agent without matching ownership state after state publication fails or raises.

**Architecture:** Treat `luna-worker.toml` and `.ai-workflow-agent-state.json` as one two-phase transaction. After Agent publication, state-hook and state-publication exits share an ownership-aware rollback path. It removes only entries whose device/inode match staged publications and restores known-legacy payloads only through no-clobber operations. Lifecycle/OSError failures return nonzero; arbitrary RuntimeError is re-raised after cleanup.

**Tech Stack:** Python 3.11+ standard library, POSIX hard links and directory FDs, `unittest`, Git.

## Global Constraints

- This is owner-authorized Sol xhigh escalation, not a reset of Task 6's two-round automatic repair budget.
- Modify only `plugins/ai-workflow/scripts/agent_lifecycle.py` and `tests/test_ai_workflow_distribution.py`.
- Preserve O_NOFOLLOW, ancestor checks, directory FDs, no-clobber hard links, fixed digest, tombstone recovery and no recursive delete.
- Rollback requires exact staged `(st_dev, st_ino)` plus digest; SHA alone cannot authorize deletion.
- A competing Agent/state remains at target or in a recoverable tombstone; never overwrite it.
- Preserve missing/current/known-legacy success, check zero-write, installer/uninstaller interfaces and macOS/Linux support.
- No dependency, global Codex mutation, merge, push or worktree deletion.

### Task 1: Exception-Safe Agent/State Publication Transaction

**Owner:** Terra `xhigh` — owner-authorized escalation implementation.

**Files:**
- Modify: `plugins/ai-workflow/scripts/agent_lifecycle.py`
- Modify: `tests/test_ai_workflow_distribution.py`

**Interfaces:**
- Produces internal transaction cleanup using target FD, staged Agent/state identities, template/state digests and optional legacy tombstone.
- Preserves `install(target_directory, *, check=False, hook=None) -> int` and `uninstall(...) -> int`.

- [ ] **Step 1: Write failing regressions.** Add tests with these exact scenarios and assertions:

```python
def test_missing_state_hook_oserror_rolls_back_agent_and_state(self):
    self.assertEqual(1, lifecycle.install(target, hook=raise_on_missing_state(OSError)))
    self.assertFalse((target / "luna-worker.toml").exists())
    self.assertFalse((target / STATE_NAME).exists())

def test_missing_state_hook_runtimeerror_rolls_back_then_reraises(self):
    with self.assertRaisesRegex(RuntimeError, "injected"):
        lifecycle.install(target, hook=raise_on_missing_state(RuntimeError))
    self.assertFalse((target / "luna-worker.toml").exists())
    self.assertFalse((target / STATE_NAME).exists())

def test_missing_state_publish_oserror_rolls_back(self):
    self.assertEqual(1, lifecycle.install(target, hook=raise_from_state_publish(OSError)))
    self.assertFalse((target / "luna-worker.toml").exists())
    self.assertFalse((target / STATE_NAME).exists())

def test_known_legacy_state_failure_restores_legacy_without_new_state(self):
    prepare_known_legacy(target)
    self.assertEqual(1, lifecycle.install(target, hook=raise_from_state_publish(OSError)))
    self.assertEqual(LEGACY_BYTES, (target / "luna-worker.toml").read_bytes())
    self.assertFalse((target / STATE_NAME).exists())
```

Also add post-link state exception, known-legacy RuntimeError re-raise, different-byte and same-digest/different-inode user replacement races.

- [ ] **Step 2: Run RED.** Run `/Users/lee/.local/bin/python3.11 -m unittest tests.test_ai_workflow_distribution -v`; expect half-installed Agent or un-restored legacy on exceptional state publication.

- [ ] **Step 3: Preserve publication identity.** Before each no-clobber publish, capture staged regular file `(st_dev, st_ino)` and SHA256. Keep these identities after Agent publication rather than clearing ownership. Existing destination entries that differ in inode, even with the same digest, are user replacements.

- [ ] **Step 4: Implement shared failure cleanup.** Use one rollback routine for state publication returning `False` and for exceptions after Agent publication. It retires/discards only owned Agent/state entries; if a state link completed before a test-injected exception, it removes only the staged-state inode. LifecycleError/OSError returns `1` after cleanup; arbitrary BaseException is re-raised after cleanup. Cleanup errors preserve entries and never mask the original exception.

- [ ] **Step 5: Cover both classifications.** Missing must leave no owned Agent/state after a failed state commit. Known-legacy must retire and verify legacy as today, remove only owned new files, then restore legacy via `_preserve_tombstone`; target races retain user data and tombstone rather than clobbering.

- [ ] **Step 6: Run focused and regression tests.** Run the distribution suite, then `/Users/lee/.local/bin/python3.11 -m unittest discover -s tests -v`; include existing EEXIST state race, check matrix, backup/tamper, descriptor-loop and unsafe ancestor tests.

- [ ] **Step 7: Run release verification.** Run `/Users/lee/.local/bin/python3.11 -m compileall -q scripts tests plugins/ai-workflow/scripts`; then `sh -n plugins/ai-workflow/scripts/install-agents.sh plugins/ai-workflow/scripts/uninstall-agents.sh`; then `sh plugins/ai-workflow/scripts/verify.sh`; then `git diff --check`.

- [ ] **Step 8: Commit.** Run `git add plugins/ai-workflow/scripts/agent_lifecycle.py tests/test_ai_workflow_distribution.py && git commit -m "fix(plugin): roll back interrupted agent publication"`.

## Plan Self-Review Results

- **Spec coverage:** missing/known-legacy, false/exceptional state failure, original exception semantics, exact identity, user races and lifecycle regressions all map to Task 1.
- **Placeholder scan:** no deferred implementation marker or unspecified test is present.
- **Type consistency:** the task reuses `install`, `_publish_no_clobber`, `_retire_and_discard`, `_preserve_tombstone`, existing state names and directory-FD primitives.
