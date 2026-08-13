# Sol-medium Final-Rework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the global Workflow default send a failed Sol-medium final acceptance to a distinct, scoped Sol-medium fixer before one distinct Sol-medium recheck and the existing owner-authorized Sol-xhigh terminal escalation.

**Architecture:** The root TOML becomes the machine-readable policy source and is mirrored byte-for-byte into the Plugin. README and the Plugin Skill publish the same bounded ladder. Existing distribution and configuration tests become the regression gate: they reject the old per-task Terra-review language and any attempt to treat Sol-medium repair as only a post-Terra-local fallback. This is a policy/distribution change; it intentionally does not claim that the current task-level repair ledger automatically dispatches whole-project final-acceptance repairs.

**Tech Stack:** Python 3.11 `unittest`, TOML (`tomllib`), shell Plugin verifier, Markdown, no new dependencies.

## Global Constraints

- Construction owners still run their frozen-envelope self-checks, target tests, negative checks, scope checks, and runtime evidence gates.
- Intermediate engineering sections receive no separate adversarial-review dispatch.
- One read-only Sol-medium performs concentrated final whole-project acceptance.
- A `REWORK` from that final acceptance grants exactly one assignment-scoped repair to a different Sol-medium identity; the accepting reviewer never writes.
- A third, different Sol-medium identity does one bounded final recheck; another `REWORK` requires owner-authorized, one-shot Sol-xhigh terminal repair without task-level review.
- No agent may merge, push, widen the frozen scope, rewrite history, or convert `BLOCKED` into acceptance.
- `config/ai_workflow.toml` and `plugins/ai-workflow/config/ai_workflow.toml` must remain byte-identical.
- Historical plans, reports, and existing ledgers remain audit evidence and are not rewritten.

---

### Task 1: Encode and publish the root final-rework policy

**Files:**
- Modify: `config/ai_workflow.toml:16-21`
- Modify: `README.md:120-147, 190-214, 275-284`
- Modify: `tests/test_ai_workflow_terra_os.py:90-116`
- Modify: `tests/test_ai_workflow_distribution.py` (new policy-language contract)

**Interfaces:**
- Consumes: `tomllib.loads(Path("config/ai_workflow.toml").read_text())`.
- Produces: `[final_acceptance_rework]` with exact keys `fixer_role`, `fixer_permission_profile`, `fixer_distinct_from_acceptor`, `recheck_role`, `recheck_distinct_from_fixer`, `terminal_escalation_role`, and `terminal_review_required`.
- Produces: public Markdown policy stating `section_self_check_only`, `Sol-medium final acceptance`, `distinct Sol-medium fixer`, `distinct Sol-medium recheck`, then owner-authorized Sol-xhigh terminal repair.

- [ ] **Step 1: Write failing root-policy tests**

```python
def test_root_final_acceptance_rework_policy_is_bounded(self):
    config = tomllib.loads((ROOT / "config/ai_workflow.toml").read_text(encoding="utf-8"))
    self.assertEqual(
        {
            "fixer_role": "sol_medium_reviewer",
            "fixer_permission_profile": "assignment-scoped-write",
            "fixer_distinct_from_acceptor": True,
            "recheck_role": "sol_medium_reviewer",
            "recheck_distinct_from_fixer": True,
            "terminal_escalation_role": "sol_xhigh",
            "terminal_review_required": False,
        },
        config["final_acceptance_rework"],
    )

def test_published_policy_uses_concentrated_final_acceptance(self):
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    self.assertIn("中间工程小节", text)
    self.assertIn("另一名 Sol-medium", text)
    self.assertNotIn("每个 task 都必须由独立、不同上下文的 Terra xhigh adversarial reviewer", text)
```

- [ ] **Step 2: Run the root-policy tests to verify they fail**

Run:

```sh
/Users/lee/.local/bin/python3.11 -m unittest \
  tests.test_ai_workflow_terra_os tests.test_ai_workflow_distribution
```

Expected: FAIL because `[final_acceptance_rework]` and the new public wording are absent while the old per-task Terra-review wording remains.

- [ ] **Step 3: Add the bounded root policy and README ladder**

```toml
[final_acceptance_rework]
fixer_role = "sol_medium_reviewer"
fixer_permission_profile = "assignment-scoped-write"
fixer_distinct_from_acceptor = true
recheck_role = "sol_medium_reviewer"
recheck_distinct_from_fixer = true
terminal_escalation_role = "sol_xhigh"
terminal_review_required = false
```

Replace the README’s per-task Terra-review ladder with the four default
stages from the approved design. State that all construction sections still
self-verify, the final Sol-medium reviewer is read-only, and the Sol-medium
fixer is assigned only the frozen final-acceptance findings.

- [ ] **Step 4: Run the root-policy tests to verify they pass**

Run:

```sh
/Users/lee/.local/bin/python3.11 -m unittest \
  tests.test_ai_workflow_terra_os tests.test_ai_workflow_distribution
```

Expected: PASS. The new exact TOML table and published sequence are required;
the stale universal Terra-review assertion is rejected.

- [ ] **Step 5: Commit the root policy**

```sh
git add config/ai_workflow.toml README.md \
  tests/test_ai_workflow_terra_os.py tests/test_ai_workflow_distribution.py
git commit -m "docs(workflow): prioritize Sol medium final rework"
```

### Task 2: Synchronize the Plugin’s frozen contract

**Files:**
- Modify: `plugins/ai-workflow/config/ai_workflow.toml`
- Modify: `plugins/ai-workflow/skills/orchestration/SKILL.md:28-46`
- Modify: `tests/test_ai_workflow_distribution.py` (Plugin wording and root-to-Plugin parity checks)

**Interfaces:**
- Consumes: byte-exact root config from Task 1 and final-rework keys.
- Produces: Plugin documentation with the same final-rework sequence and no
  claim that each construction section requires an independent Terra review.

- [ ] **Step 1: Write the failing Plugin-contract test**

```python
def test_plugin_skill_publishes_final_sol_medium_rework_ladder(self):
    skill = (PLUGIN / "skills/orchestration/SKILL.md").read_text(encoding="utf-8")
    self.assertIn("Intermediate engineering sections", skill)
    self.assertIn("different Sol-medium fixer", skill)
    self.assertIn("different Sol-medium recheck", skill)
    self.assertNotIn("Every task needs an independent Terra", skill)
    self.assertEqual(
        (ROOT / "config/ai_workflow.toml").read_bytes(),
        (PLUGIN / "config/ai_workflow.toml").read_bytes(),
    )
```

- [ ] **Step 2: Run the Plugin-contract test to verify it fails**

Run:

```sh
/Users/lee/.local/bin/python3.11 -m unittest \
  tests.test_ai_workflow_distribution.DistributionContractTest
```

Expected: FAIL because the Plugin still describes per-task Terra review and
does not mirror the new TOML table.

- [ ] **Step 3: Mirror the TOML and update Plugin Skill language**

Copy the complete root TOML to the Plugin mirror without unrelated edits.
Replace the frozen role/lifecycle bullets with the approved final-rework
sequence. Keep Luna’s no-review/no-approval boundary, Terra xhigh
construction ownership, human owner authorization, and Sol-xhigh terminal
limits unchanged.

- [ ] **Step 4: Run the Plugin-contract test and verifier to verify they pass**

Run:

```sh
/Users/lee/.local/bin/python3.11 -m unittest \
  tests.test_ai_workflow_distribution.DistributionContractTest
sh plugins/ai-workflow/scripts/verify.sh
```

Expected: PASS. The mirror is byte-identical and the verifier accepts the
release.

- [ ] **Step 5: Commit the Plugin contract**

```sh
git add plugins/ai-workflow/config/ai_workflow.toml \
  plugins/ai-workflow/skills/orchestration/SKILL.md \
  tests/test_ai_workflow_distribution.py
git commit -m "docs(plugin): publish Sol medium final rework"
```

### Task 3: Verify the global default change

**Files:**
- Modify: none

**Interfaces:**
- Consumes: Task 1 root policy and Task 2 Plugin mirror.
- Produces: reproducible verification evidence only; no extra production
  semantics or live model invocation.

- [ ] **Step 1: Run the focused policy and distribution suites**

Run:

```sh
/Users/lee/.local/bin/python3.11 -m unittest \
  tests.test_ai_workflow_terra_os \
  tests.test_ai_workflow_distribution
```

Expected: PASS.

- [ ] **Step 2: Run full, compile, and release gates**

Run:

```sh
/Users/lee/.local/bin/python3.11 -m unittest discover -s tests
/Users/lee/.local/bin/python3.11 -m compileall -q config scripts tests \
  plugins/ai-workflow/runtime plugins/ai-workflow/scripts
sh plugins/ai-workflow/scripts/verify.sh
python3 /Users/lee/.codex/skills/skill-creator/scripts/quick_validate.py \
  plugins/ai-workflow/skills/orchestration
git diff --check HEAD~2..HEAD
```

Expected: every command exits 0. Do not call a live model, merge, or push.

- [ ] **Step 3: Record the policy limitation**

State in the final handoff that the policy and distribution contract are
enforced as documentation/configuration and regression tests. The existing
task-level repair ledger is not represented as a whole-project final-
acceptance controller, so automatic runtime dispatch of the new fixer remains
out of scope until a later controller task consumes the frozen final-
acceptance receipt.
