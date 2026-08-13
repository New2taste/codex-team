# Task 3 — Team Call publication and release mirror

## Conclusion

`SUPPORTED` — the Plugin release now carries the Team Call runtime as an
exact root mirror, the release verifier rejects a changed copy, and the public
contract documents only the bounded direct-call surface.

## Authorized scope and reconciliation

The original brief authorized:

- `README.md`
- `plugins/ai-workflow/runtime/ai_workflow_team_call.py`
- `plugins/ai-workflow/scripts/verify.sh`
- `plugins/ai-workflow/skills/orchestration/SKILL.md`
- `tests/test_ai_workflow_distribution.py`

At the start of this task, the focused distribution RED showed an existing
root/Plugin parity failure: `scripts/ai_workflow.py` had already changed while
`plugins/ai-workflow/runtime/ai_workflow.py` had not. This caused both the
existing byte-exact distribution assertion and a clean copied-release
verifier invocation to fail before the Task 3 changes.

The owner explicitly reconciled that cross-task dependency and minimally
extended this task to copy the current root `scripts/ai_workflow.py` to its
Plugin runtime mirror byte-for-byte. No logic in either runtime was changed;
the additional file is solely the required release-parity copy. No other
scope was expanded.

## TDD evidence

1. Added the Team Call publication/mirror test and the copied-release tamper
   test in `DistributionContractTest`.
2. RED command:

   ```sh
   /Users/lee/.local/bin/python3.11 -m unittest -v \
     tests.test_ai_workflow_distribution.DistributionContractTest
   ```

   The new tests failed because
   `plugins/ai-workflow/runtime/ai_workflow_team_call.py` did not exist. The
   same run also exposed the pre-existing `ai_workflow.py` root/Plugin parity
   failure described above.
3. GREEN implementation: copied both authorized mirrors byte-for-byte, added
   `ai_workflow_team_call.py` to the Plugin verifier and generic runtime-copy
   assertions, and added the bounded public documentation.
4. GREEN focused distribution suite: 15 tests passed. The Team Call copied
   release test first verifies a clean release exits zero, then appends text
   only to `runtime/ai_workflow_team_call.py` and verifies a nonzero exit with
   `runtime copy differs`.

## Published contract

`README.md` and the Plugin orchestration skill publish only:

- the three accepted `team call` directive forms;
- `DIRECT_L0`, `DIRECT_L1`, `PLAN_REQUIRED`, and `BLOCKED` dispositions;
- one global active worker by default, with no parallel-agent promise;
- fixed-argv L0 controller/no-model behavior;
- Luna-only read-only L1 evidence extraction;
- the frozen-envelope plan fallback and human owner gates; and
- unchanged prohibitions on Luna review, approval, final acceptance, automatic
  merge, and automatic push.

## Verification evidence

| Check | Result |
|---|---|
| Focused `DistributionContractTest` | 15 passed |
| Team Call mirror + clean/tampered copied release tests | 2 passed |
| Full unit suite | 389 passed; 8 pre-existing conditional skips |
| `compileall` for config, scripts, tests, Plugin runtime/scripts | exit 0 |
| Plugin `verify.sh` | exit 0 |
| Plugin manifest JSON parse | exit 0 |
| Orchestration skill quick validation | `Skill is valid!` |
| Plugin shell syntax checks | exit 0 |
| `git diff --check` | exit 0 |

The brief's two validator paths under
`/Users/lee/.codex/plugins/cache/openai-bundled/sites/0.1.34/skills/` are not
present in this environment. The requested Plugin validator could therefore
not be invoked. The installed equivalent skill validator exists at
`/Users/lee/.codex/skills/skill-creator/scripts/quick_validate.py`; running it
with the system `python3` (which supplies PyYAML) passed. Plugin manifest JSON
was additionally parsed, and the Plugin's own verifier passed.

## Boundaries and omissions

- No live model was invoked.
- No extra agent, intermediate task-level adversarial review, approval, final
  acceptance, merge, or push was requested or performed.
- The required final Sol-medium whole-project acceptance remains outside this
  bounded task.
